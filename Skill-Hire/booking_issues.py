"""Booking-scoped issue threads with participant ownership and admin review."""
from datetime import datetime
import re
from flask import jsonify, request
from db import get_db, for_update, is_postgres, id_column_sql, foreign_id_sql

CATEGORIES = {
    'hirer': {'worker_no_show', 'incomplete_work', 'payment_issue', 'other'},
    'worker': {'hirer_unavailable', 'wrong_address', 'cash_not_received', 'payment_issue', 'other'},
}


def ensure_schema(conn):
    conn.execute(f"""CREATE TABLE IF NOT EXISTS booking_issues (
        id {id_column_sql()}, booking_id {foreign_id_sql()} NOT NULL REFERENCES bookings(id),
        reporter_role TEXT NOT NULL, reporter_id {foreign_id_sql()} NOT NULL,
        category TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
        resolution TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(booking_id,reporter_role,category))""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS booking_issue_events (
        id {id_column_sql()}, issue_id {foreign_id_sql()} NOT NULL REFERENCES booking_issues(id),
        actor_role TEXT NOT NULL, actor_id {foreign_id_sql()}, event_type TEXT NOT NULL,
        body TEXT NOT NULL, request_key TEXT, created_at TEXT NOT NULL,
        UNIQUE(issue_id,actor_role,request_key))""")
    conn.execute('CREATE INDEX IF NOT EXISTS booking_issues_queue ON booking_issues(status,updated_at)')


def install_routes(app, env):
    def participant(conn, booking_id, role, lock=False):
        column='hirer_id' if role=='hirer' else 'worker_id'
        identifier=env['current_hirer_id' if role=='hirer' else 'current_worker_id']()
        sql=f'SELECT * FROM bookings WHERE id=? AND {column}=?'
        return conn.execute(for_update(sql) if lock else sql,(booking_id,identifier)).fetchone()

    def auth(role):
        return env['require_login' if role=='hirer' else 'require_worker_login']()

    def lock(conn):
        if not is_postgres(): conn.execute('BEGIN IMMEDIATE')

    def event(conn, issue_id, role, identifier, kind, body, key=None):
        conn.execute('INSERT INTO booking_issue_events(issue_id,actor_role,actor_id,event_type,body,request_key,created_at) VALUES (?,?,?,?,?,?,?)',
                     (issue_id,role,identifier,kind,body,key,datetime.utcnow().isoformat()))

    def public_issue(conn, row):
        result=dict(row)
        result.pop('reporter_id',None)
        result['events']=[dict(x) for x in conn.execute('SELECT actor_role,event_type,body,created_at FROM booking_issue_events WHERE issue_id=? ORDER BY id DESC LIMIT 50',(row['id'],)).fetchall()][::-1]
        return result

    def notify(conn, booking, text):
        for role in ('hirer','worker'):
            env['add_in_app_notification'](conn,role,booking[role+'_id'],'booking_issue','Booking issue update',text,booking['id'])

    def issues(booking_id, role):
        error=auth(role)
        if error:return error
        data=request.get_json(silent=True) if request.method=='POST' else None
        if request.method=='POST':
            if not isinstance(data,dict):return jsonify(error='Issue details are required'),400
            category=data.get('category');description=data.get('description')
            if not isinstance(category,str) or category not in CATEGORIES[role]:return jsonify(error='Choose a valid issue category'),400
            if not isinstance(description,str) or not 10<=len(description.strip())<=2000:return jsonify(error='Describe the issue in 10–2000 characters'),400
            description=description.strip()
        conn=get_db()
        try:
            if request.method=='POST':lock(conn)
            booking=participant(conn,booking_id,role,request.method=='POST')
            if not booking:return jsonify(error='Booking not found'),404
            if request.method=='GET':
                rows=conn.execute('SELECT * FROM booking_issues WHERE booking_id=? ORDER BY id DESC',(booking_id,)).fetchall()
                return jsonify([public_issue(conn,row) for row in rows])
            existing=conn.execute('SELECT * FROM booking_issues WHERE booking_id=? AND reporter_role=? AND category=?',(booking_id,role,category)).fetchone()
            if existing:return jsonify(issue=public_issue(conn,existing),replayed=True),200
            now=datetime.utcnow().isoformat()
            identifier=booking[role+'_id']
            cur=conn.execute('INSERT INTO booking_issues(booking_id,reporter_role,reporter_id,category,description,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',(booking_id,role,identifier,category,description,now,now))
            issue_id=cur.lastrowid
            event(conn,issue_id,role,identifier,'reported',description)
            notify(conn,booking,f'Issue #{issue_id} reported for booking #{booking_id}. Open Booking Help to review.')
            row=conn.execute('SELECT * FROM booking_issues WHERE id=?',(issue_id,)).fetchone()
            result=public_issue(conn,row);conn.commit()
            return jsonify(issue=result,replayed=False),201
        finally:conn.close()

    def reply(booking_id, issue_id, role):
        error=auth(role)
        if error:return error
        data=request.get_json(silent=True) or {}
        if not isinstance(data,dict):return jsonify(error='Reply details are required'),400
        body=data.get('body');key=data.get('request_key')
        if not isinstance(body,str) or not 1<=len(body.strip())<=2000:return jsonify(error='Reply must contain 1–2000 characters'),400
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',key):return jsonify(error='A valid reply request key is required'),400
        body=body.strip();conn=get_db()
        try:
            lock(conn);booking=participant(conn,booking_id,role,True)
            if not booking:return jsonify(error='Booking not found'),404
            row=conn.execute(for_update('SELECT * FROM booking_issues WHERE id=? AND booking_id=?'),(issue_id,booking_id)).fetchone()
            if not row:return jsonify(error='Issue not found'),404
            old=conn.execute('SELECT body FROM booking_issue_events WHERE issue_id=? AND actor_role=? AND request_key=?',(issue_id,role,key)).fetchone()
            if old:
                if old['body']!=body:return jsonify(error='This reply key belongs to a different message'),409
                return jsonify(issue=public_issue(conn,row),replayed=True)
            reopening=row['status']=='resolved' and row['reporter_role']==role
            event(conn,issue_id,role,booking[role+'_id'],'reopened' if reopening else 'reply',body,key)
            conn.execute('UPDATE booking_issues SET status=?,updated_at=? WHERE id=?',('open' if reopening else row['status'],datetime.utcnow().isoformat(),issue_id))
            notify(conn,booking,f'New issue reply for booking #{booking_id}. Open Booking Help to review.')
            row=conn.execute('SELECT * FROM booking_issues WHERE id=?',(issue_id,)).fetchone()
            result=public_issue(conn,row);conn.commit()
            return jsonify(issue=result,replayed=False)
        finally:conn.close()

    def admin_issues():
        error=env['_check_admin_session']()
        if error:return error
        conn=get_db()
        try:
            rows=conn.execute("""SELECT i.*,h.name AS hirer_name,w.name AS worker_name,b.status AS booking_status
                FROM booking_issues i JOIN bookings b ON b.id=i.booking_id
                JOIN hirers h ON h.id=b.hirer_id JOIN workers w ON w.id=b.worker_id
                ORDER BY CASE WHEN i.status='resolved' THEN 1 ELSE 0 END,i.updated_at DESC LIMIT 200""").fetchall()
            return jsonify([{k:v for k,v in dict(row).items() if k!='reporter_id'} for row in rows])
        finally:conn.close()

    def admin_issue(issue_id):
        error=env['_check_admin_session']()
        if error:return error
        if request.method=='POST':
            error=env['_check_admin_csrf']()
            if error:return error
            data=request.get_json(silent=True) or {}
            if not isinstance(data,dict):return jsonify(error='Review details are required'),400
            status=data.get('status');resolution=data.get('resolution','');expected=data.get('expected_updated_at')
            if status not in ('open','in_review','resolved') or not isinstance(resolution,str) or len(resolution.strip())>2000:return jsonify(error='Valid status and resolution note are required'),400
            resolution=resolution.strip()
            if status=='resolved' and len(resolution)<10:return jsonify(error='Explain the resolution in at least 10 characters'),400
            if not isinstance(expected,str) or not expected:return jsonify(error='Refresh this issue before reviewing'),400
        conn=get_db()
        try:
            if request.method=='POST':lock(conn)
            initial=conn.execute('SELECT booking_id FROM booking_issues WHERE id=?',(issue_id,)).fetchone()
            if not initial:return jsonify(error='Issue not found'),404
            if request.method=='POST':conn.execute(for_update('SELECT id FROM bookings WHERE id=?'),(initial['booking_id'],)).fetchone()
            row=conn.execute(for_update('SELECT * FROM booking_issues WHERE id=?') if request.method=='POST' else 'SELECT * FROM booking_issues WHERE id=?',(issue_id,)).fetchone()
            if not row:return jsonify(error='Issue not found'),404
            booking=conn.execute('SELECT * FROM bookings WHERE id=?',(row['booking_id'],)).fetchone()
            if request.method=='GET':
                messages=[dict(x) for x in conn.execute('SELECT sender_role,body,created_at FROM messages WHERE booking_id=? ORDER BY id DESC LIMIT 100',(booking['id'],)).fetchall()][::-1]
                return jsonify(issue=public_issue(conn,row),booking=env['public_booking'](booking),messages=messages)
            if row['updated_at']!=expected:
                if row['status']==status and (row['resolution'] or '')==resolution:return jsonify(issue=public_issue(conn,row),replayed=True)
                return jsonify(error='Issue changed since it was opened. Refresh and review the latest replies.'),409
            if row['status']==status and (row['resolution'] or '')==resolution:return jsonify(issue=public_issue(conn,row),replayed=True)
            conn.execute('UPDATE booking_issues SET status=?,resolution=?,updated_at=? WHERE id=?',(status,resolution,datetime.utcnow().isoformat(),issue_id))
            event(conn,issue_id,'admin',None,status,resolution or f'Issue marked {status}')
            notify(conn,booking,f'Issue #{issue_id} is {status.replace("_"," ")} for booking #{booking["id"]}. Open Booking Help for the resolution.')
            row=conn.execute('SELECT * FROM booking_issues WHERE id=?',(issue_id,)).fetchone()
            result=public_issue(conn,row);conn.commit()
            return jsonify(issue=result,replayed=False)
        finally:conn.close()

    for role,prefix in [('hirer','/api/bookings'),('worker','/api/worker/bookings')]:
        app.add_url_rule(prefix+'/<int:booking_id>/issues','booking_issues_'+role,lambda booking_id,role=role:issues(booking_id,role),methods=['GET','POST'])
        app.add_url_rule(prefix+'/<int:booking_id>/issues/<int:issue_id>/reply','booking_issue_reply_'+role,lambda booking_id,issue_id,role=role:reply(booking_id,issue_id,role),methods=['POST'])
    app.add_url_rule('/api/admin/issues','admin_booking_issues',admin_issues)
    app.add_url_rule('/api/admin/issues/<int:issue_id>','admin_booking_issue',admin_issue,methods=['GET','POST'])

"""Latest travel GPS, scoped to assigned worker and booking hirer."""
from datetime import datetime
import math
import os
import time
import requests
import secrets
from flask import jsonify, request
from db import get_db, for_update, is_postgres, foreign_id_sql


def ensure_schema(conn):
    if is_postgres():conn.execute('SELECT pg_advisory_xact_lock(?)',(74219013,))
    conn.execute(f"""CREATE TABLE IF NOT EXISTS booking_travel_locations (
        booking_id {foreign_id_sql()} PRIMARY KEY REFERENCES bookings(id),
        latitude DOUBLE PRECISION NOT NULL,longitude DOUBLE PRECISION NOT NULL,accuracy REAL NOT NULL,
        gps_timestamp DOUBLE PRECISION NOT NULL,speed_mps REAL,updated_at TEXT NOT NULL,
        route_seconds REAL,route_metres REAL,route_checked_at TEXT,sharing_token TEXT)""")


def validate(data, parse_coordinates):
    if not isinstance(data,dict):raise ValueError('A fresh GPS location is required to start or share travel')
    lat,lng=parse_coordinates(data.get('latitude'),data.get('longitude'))
    if lat is None:raise ValueError('Allow location access before heading out')
    for key in ('accuracy','gps_timestamp','speed'):
        if isinstance(data.get(key),bool):raise ValueError('Invalid GPS reading')
    try:
        accuracy=float(data.get('accuracy'));timestamp=float(data.get('gps_timestamp'))
        speed=None if data.get('speed') is None else float(data['speed'])
    except (ValueError,TypeError,OverflowError):raise ValueError('Valid GPS accuracy and timestamp are required')
    if not math.isfinite(accuracy) or not 0<=accuracy<=1000:raise ValueError('Travel GPS accuracy must be 1000 metres or better. Enable precise location and retry. Arrival still needs 100 metres or better.')
    if not math.isfinite(timestamp) or not -30000<=time.time()*1000-timestamp<=120000:raise ValueError('Travel GPS is stale. Get a fresh location and retry.')
    if speed is not None and (not math.isfinite(speed) or not 0<=speed<=70):raise ValueError('Invalid GPS speed')
    return {'latitude':lat,'longitude':lng,'accuracy':accuracy,'gps_timestamp':timestamp,'speed_mps':speed}


def save(conn,booking_id,fix):
    previous=conn.execute('SELECT gps_timestamp,latitude,longitude FROM booking_travel_locations WHERE booking_id=?',(booking_id,)).fetchone()
    if previous and fix['gps_timestamp']<previous['gps_timestamp']:raise ValueError('An older location cannot replace the latest GPS reading')
    if previous and fix['gps_timestamp']==previous['gps_timestamp'] and (not math.isclose(fix['latitude'],previous['latitude'],abs_tol=1e-6) or not math.isclose(fix['longitude'],previous['longitude'],abs_tol=1e-6)):raise ValueError('This GPS timestamp already belongs to a different reading')
    conn.execute("""INSERT INTO booking_travel_locations(booking_id,latitude,longitude,accuracy,gps_timestamp,speed_mps,updated_at)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(booking_id) DO UPDATE SET latitude=excluded.latitude,longitude=excluded.longitude,
        accuracy=excluded.accuracy,gps_timestamp=excluded.gps_timestamp,speed_mps=excluded.speed_mps,updated_at=excluded.updated_at""",
        (booking_id,fix['latitude'],fix['longitude'],fix['accuracy'],fix['gps_timestamp'],fix['speed_mps'],datetime.utcnow().isoformat()))


def activate(conn,booking_id):
    row=conn.execute('SELECT sharing_token FROM booking_travel_locations WHERE booking_id=?',(booking_id,)).fetchone()
    existing=row and row['sharing_token']
    token=existing or secrets.token_hex(16)
    if not existing:conn.execute('UPDATE booking_travel_locations SET route_seconds=NULL,route_metres=NULL,route_checked_at=NULL WHERE booking_id=?',(booking_id,))
    conn.execute('UPDATE booking_travel_locations SET sharing_token=? WHERE booking_id=?',(token,booking_id))
    return token


def google_route(fix,booking):
    # Sending precise coordinates to Google is opt-in by the operator, never an implicit fallback.
    if os.environ.get('TRAVEL_ROUTING_PROVIDER')!='google' or os.environ.get('TRAVEL_ROUTING_COORDINATES_ALLOWED')!='true':return None
    key=os.environ.get('GOOGLE_ROUTES_API_KEY','')
    if not key:return None
    try:
        response=requests.post('https://routes.googleapis.com/directions/v2:computeRoutes',
            headers={'X-Goog-Api-Key':key,'X-Goog-FieldMask':'routes.duration,routes.distanceMeters'},
            json={'origin':{'location':{'latLng':{'latitude':fix['latitude'],'longitude':fix['longitude']}}},
                  'destination':{'location':{'latLng':{'latitude':booking['service_latitude'],'longitude':booking['service_longitude']}}},
                  'travelMode':'DRIVE','routingPreference':'TRAFFIC_AWARE'},timeout=5)
        response.raise_for_status();route=response.json()['routes'][0]
        seconds=float(route['duration'].rstrip('s'));metres=float(route['distanceMeters'])
        if not math.isfinite(seconds) or not math.isfinite(metres) or seconds<0 or metres<0:return None
        return seconds,metres
    except (requests.RequestException,ValueError,KeyError,IndexError,TypeError,AttributeError):return None


def install_routes(app,env):
    def update(booking_id):
        error=env['require_worker_login']()
        if error:return error
        if request.method!='DELETE':
            try:fix=validate(request.get_json(silent=True),env['parse_optional_coordinates'])
            except ValueError as exc:return jsonify(error=str(exc)),400
        conn=get_db()
        try:
            if not is_postgres():conn.execute('BEGIN IMMEDIATE')
            booking=conn.execute(for_update('SELECT * FROM bookings WHERE id=? AND worker_id=?'),(booking_id,env['current_worker_id']())).fetchone()
            if not booking:return jsonify(error='Booking not found'),404
            if request.method=='DELETE':
                conn.execute('UPDATE booking_travel_locations SET sharing_token=NULL WHERE booking_id=?',(booking_id,));conn.commit();return jsonify(sharing=False)
            if booking['status']!='en_route':return jsonify(error='Live location can be shared only while travelling',sharing=False),409
            shared=conn.execute('SELECT sharing_token FROM booking_travel_locations WHERE booking_id=?',(booking_id,)).fetchone()
            supplied=(request.get_json(silent=True) or {}).get('sharing_token')
            if not shared or not shared['sharing_token'] or not isinstance(supplied,str) or not secrets.compare_digest(shared['sharing_token'],supplied):return jsonify(error='Location sharing is paused. Resume sharing from the worker portal.'),403
            try:save(conn,booking_id,fix)
            except ValueError as exc:return jsonify(error=str(exc)),409
            conn.commit();return jsonify(ok=True,sharing=True)
        finally:conn.close()

    def read(booking_id):
        error=env['require_login']()
        if error:return error
        conn=get_db()
        try:
            booking=conn.execute('SELECT * FROM bookings WHERE id=? AND hirer_id=?',(booking_id,env['current_hirer_id']())).fetchone()
            if not booking:return jsonify(error='Booking not found'),404
            if booking['status']!='en_route':return jsonify(sharing=False,location=None,eta_minutes=None)
            row=conn.execute('SELECT * FROM booking_travel_locations WHERE booking_id=?',(booking_id,)).fetchone()
            if not row or not row['sharing_token']:return jsonify(sharing=False,location=None,eta_minutes=None,message='Worker location sharing is paused or has not started')
            fix=dict(row);age=max(0,(time.time()*1000-fix['gps_timestamp'])/1000);fresh=age<=120
            distance=None;eta=None;basis=None
            if booking['service_latitude'] is not None and booking['service_longitude'] is not None:
                distance=env['haversine_distance_km'](fix['latitude'],fix['longitude'],booking['service_latitude'],booking['service_longitude'])
            route_enabled=os.environ.get('TRAVEL_ROUTING_PROVIDER')=='google' and os.environ.get('TRAVEL_ROUTING_COORDINATES_ALLOWED')=='true' and bool(os.environ.get('GOOGLE_ROUTES_API_KEY'))
            if fresh and fix['accuracy']<=100 and distance is not None and distance<=150:
                if route_enabled:
                    checked=datetime.fromisoformat(fix['route_checked_at']) if fix['route_checked_at'] else None
                    if not checked or (datetime.utcnow()-checked).total_seconds()>=60:
                        route=google_route(fix,booking);seconds,metres=route if route else (None,None)
                        conn.execute('UPDATE booking_travel_locations SET route_seconds=?,route_metres=?,route_checked_at=? WHERE booking_id=? AND gps_timestamp=?',(seconds,metres,datetime.utcnow().isoformat(),booking_id,fix['gps_timestamp']));conn.commit()
                        fix['route_seconds']=seconds;fix['route_metres']=metres
                    if fix['route_seconds'] is not None:eta=max(1,math.ceil(fix['route_seconds']/60));basis='traffic_route'
                if eta is None and fix['speed_mps'] is not None and fix['speed_mps']>=1:
                    eta=max(1,math.ceil(distance*1000/fix['speed_mps']/60));basis='straight_line_current_speed'
            latest=conn.execute('SELECT status FROM bookings WHERE id=?',(booking_id,)).fetchone()
            if latest['status']!='en_route':return jsonify(sharing=False,location=None,eta_minutes=None)
            return jsonify(sharing=True,fresh=fresh,age_seconds=round(age),location={k:fix[k] for k in ('latitude','longitude','accuracy','gps_timestamp')},
                           destination={'latitude':booking['service_latitude'],'longitude':booking['service_longitude']},distance_km=round(distance,2) if distance is not None else None,
                           eta_minutes=eta,eta_basis=basis,route_distance_km=round(fix['route_metres']/1000,2) if basis=='traffic_route' and fix['route_metres'] is not None else None,
                           message='Live GPS updates require the worker portal to remain open. ETA is an estimate.' if fresh else 'Location is stale. Last known position is shown; ETA is unavailable.')
        finally:conn.close()
    @app.after_request
    def travel_no_cache(response):
        if '/travel-location' in request.path or request.path.endswith('/start-travel'):
            response.headers['Cache-Control']='no-store, private'
        return response

    app.add_url_rule('/api/worker/bookings/<int:booking_id>/travel-location','worker_travel_location',update,methods=['POST','DELETE'])
    app.add_url_rule('/api/bookings/<int:booking_id>/travel-location','hirer_travel_location',read)

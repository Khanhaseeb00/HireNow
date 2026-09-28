"""
Run once to populate the workers table with sample data, so the API has
something to log in with and test against while you build the real
worker-onboarding flow.

Every seeded worker gets the SAME demo password so you can log into the
worker portal (/worker) with any of their phone numbers immediately:

    password: kaamgar123

Usage:  python seed_data.py
"""
from werkzeug.security import generate_password_hash
from db import get_db, init_db

DEMO_PASSWORD = "kaamgar123"

# name, phone, skill, skills_detail, city, daily_wage, rating, jobs, exp_years, verification_status, hourly_wage, distance_km, availability
WORKERS = [
    ("Ramesh Yadav", "9820011122", "Mistri / Mason", "Deewar chinai, Plaster, Tiling base", "Andheri, Mumbai", 800, 4.7, 132, 9, "verified", 100, 2.4, "today"),
    ("Suresh Kumar", "9911022233", "Electrician", "Wiring, MCB fitting, Fan/light installation", "Dwarka, Delhi", 700, 4.5, 98, 6, "verified", 90, 4.1, "today"),
    ("Anita Devi", "9845033344", "Painter", "Wall painting, Waterproofing, Texture work", "Whitefield, Bengaluru", 650, 4.8, 210, 11, "verified", 85, 1.8, "today"),
    ("Mohan Lal", "9820044455", "Plumber", "Pipeline fitting, Leak repair, Bathroom fitting", "Andheri, Mumbai", 720, 4.3, 76, 5, "unverified", 95, 5.6, "tomorrow"),
    ("Deepak Singh", "9903055566", "Carpenter", "Furniture banana, Door/window fitting, Modular work", "Salt Lake, Kolkata", 780, 4.6, 154, 8, "verified", 100, 3.2, "today"),
    ("Iqbal Ansari", "9866066677", "Welder", "Gate/grill welding, Steel fabrication", "Banjara Hills, Hyderabad", 750, 4.4, 60, 7, "pending", 95, 6.7, "tomorrow"),
    ("Geeta Sharma", "9822077788", "Helper / Majdoor", "Loading-unloading, Site cleaning, General madad", "Kothrud, Pune", 500, 4.2, 88, 3, "verified", 65, 2.9, "today"),
    ("Vijay Prajapati", "9911088899", "Tile Worker", "Floor tiling, Wall tiling, Marble fitting", "Dwarka, Delhi", 680, 4.6, 140, 10, "verified", 90, 4.5, "today"),
    ("Farhan Sheikh", "9845099900", "AC Technician", "AC installation, Gas filling, Servicing", "Whitefield, Bengaluru", 690, 4.5, 102, 6, "verified", 90, 3.8, "tomorrow"),
]


def seed():
    init_db()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM workers")
    if cur.fetchone()["c"] > 0:
        print("Workers table already has data — skipping seed. "
              "Delete kaamgar.db if you want to reseed from scratch.")
        conn.close()
        return

    pw_hash = generate_password_hash(DEMO_PASSWORD)
    rows = [(w[0], w[1], pw_hash, w[2], w[3], w[4], w[5], w[6], w[7], w[8], w[9], w[10], w[11], w[12]) for w in WORKERS]

    cur.executemany(
        """INSERT INTO workers
           (name, phone, password_hash, skill, skills_detail, city, daily_wage, rating, jobs_completed, experience_years, verification_status, hourly_wage, distance_km, availability)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()
    print(f"Seeded {len(WORKERS)} workers into kaamgar.db")
    print(f"All workers can log into /worker with their phone number and password '{DEMO_PASSWORD}'")


if __name__ == "__main__":
    seed()

# check_database.py
import sqlite3
import json

# Connect to the database
conn = sqlite3.connect('messages.db')  # Replace with your actual DB file
cursor = conn.cursor()

# Query the gitlab_events table
cursor.execute("SELECT * FROM gitlab_events ORDER BY id DESC LIMIT 5")
rows = cursor.fetchall()

for row in rows:
    print(f"ID: {row[0]}")
    print(f"Dev: {row[1]}")
    print(f"Timestamp: {row[2]}")
    print(f"Type: {row[3]}")
    print(f"Payload: {json.loads(row[4])}")
    print("-" * 50)

conn.close()
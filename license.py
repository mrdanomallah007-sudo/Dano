import secrets
import string
import requests
from datetime import datetime, timedelta, timezone

FIREBASE_URL = "https://my-cloning-tool-default-rtdb.firebaseio.com/"

def make_key():
    alphabet = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4)]
    return "AHB-" + "-".join(parts)

def expiry_for(days):
    if days == "lifetime":
        return "Lifetime"
    return (datetime.now(timezone.utc) + timedelta(days=int(days))).strftime("%Y-%m-%d")

def create_license(days, name=""):
    key = make_key()
    expiry = expiry_for(days)
    data = {
        "name": name or "User",
        "expiry": expiry,
        "hwid": ""
    }
    r = requests.put(f"{FIREBASE_URL}keys/{key}.json", json=data, timeout=15)
    r.raise_for_status()
    print(f"\nKEY: {key}")
    print(f"TYPE: {days}")
    print(f"EXPIRY: {expiry}\n")
    return key

def main():
    print("=== AHB LICENSE GENERATOR ===")
    print("1) 2 Days")
    print("2) 7 Days")
    print("3) 15 Days")
    print("4) 30 Days")
    print("5) Lifetime")
    choice = input("Select: ").strip()
    mapping = {"1": 2, "2": 7, "3": 15, "4": 30, "5": "lifetime"}
    if choice not in mapping:
        print("Invalid choice.")
        return
    name = input("User name (optional): ").strip()
    create_license(mapping[choice], name)

if __name__ == "__main__":
    main()

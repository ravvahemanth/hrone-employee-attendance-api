import os

# Set test database configuration before any app or test modules are imported
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "attendance_db_test"

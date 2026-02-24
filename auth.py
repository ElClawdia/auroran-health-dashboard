#!/usr/bin/env python3
"""
Authentication module for Health Dashboard
Handles user login, session management, and password hashing
"""

import hashlib
import secrets
import json
from pathlib import Path
from functools import wraps
from flask import session, redirect, url_for, request, jsonify

# User data file (simple JSON storage for now)
USERS_FILE = Path(__file__).parent / "users.json"

# Default user (registration disabled, so we pre-create this user)
DEFAULT_USERS = {
    "elsonico": {
        "full_name": "Tapio Vaattanen",
        "email": "vaattanen@gmail.com",
        "username": "elsonico",
        "dob": None,  # YYYY-MM-DD
        "height_cm": None,
        "initial_weight_kg": None,
        "password_hash": None,  # Will be set on first load
        "password_raw": "cTfp!&!yt%jHU8&2@f"  # Only used for initial hash
    }
}


def hash_password(password: str, salt: str = None) -> tuple:
    """Hash password with salt using SHA-256"""
    if salt is None:
        salt = secrets.token_hex(16)
    hash_obj = hashlib.sha256((salt + password).encode())
    return hash_obj.hexdigest(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    """Verify password against stored hash"""
    computed_hash, _ = hash_password(password, salt)
    return secrets.compare_digest(computed_hash, password_hash)


def load_users() -> dict:
    """Load users from JSON file, creating default if needed"""
    if USERS_FILE.exists():
        with open(USERS_FILE) as f:
            return json.load(f)
    
    # Create default users with hashed passwords
    users = {}
    for username, data in DEFAULT_USERS.items():
        password_hash, salt = hash_password(data["password_raw"])
        users[username] = {
            "full_name": data["full_name"],
            "email": data["email"],
            "username": username,
            "dob": data.get("dob"),
            "height_cm": data.get("height_cm"),
            "initial_weight_kg": data.get("initial_weight_kg"),
            "password_hash": password_hash,
            "salt": salt
        }
    
    save_users(users)
    return users


def save_users(users: dict):
    """Save users to JSON file"""
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)


def get_user(username: str) -> dict:
    """Get user by username"""
    users = load_users()
    return users.get(username)


def authenticate(username: str, password: str) -> dict:
    """Authenticate user, returns user dict if successful, None otherwise"""
    user = get_user(username)
    if not user:
        return None
    
    if verify_password(password, user["password_hash"], user["salt"]):
        return user
    return None


def update_user(username: str, updates: dict) -> bool:
    """Update user data"""
    users = load_users()
    if username not in users:
        return False
    
    # Handle password change
    if "new_password" in updates:
        password_hash, salt = hash_password(updates["new_password"])
        users[username]["password_hash"] = password_hash
        users[username]["salt"] = salt
        del updates["new_password"]
    
    # Update other fields
    for key, value in updates.items():
        if key in ["full_name", "email", "dob", "height_cm", "initial_weight_kg"]:
            users[username][key] = value
    
    save_users(users)
    return True


def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            # Check if it's an API request
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated_function


def get_current_user() -> dict:
    """Get currently logged in user from session"""
    if "user" not in session:
        return None
    return get_user(session["user"])

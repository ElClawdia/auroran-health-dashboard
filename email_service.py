#!/usr/bin/env python3
"""
Email Service for Health Dashboard
Sends verification emails for password changes
"""

import smtplib
import ssl
import secrets
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

from config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM_EMAIL, SMTP_FROM_NAME, SMTP_USE_SSL

# Email configuration
FROM_EMAIL = SMTP_FROM_EMAIL
FROM_NAME = SMTP_FROM_NAME

# Token storage file
TOKENS_FILE = Path(__file__).parent / "password_reset_tokens.json"
TOKEN_EXPIRY_HOURS = 24


def load_tokens() -> dict:
    """Load password reset tokens from file"""
    if TOKENS_FILE.exists():
        try:
            with open(TOKENS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_tokens(tokens: dict):
    """Save password reset tokens to file"""
    with open(TOKENS_FILE, 'w') as f:
        json.dump(tokens, f, indent=2)


def generate_reset_token(username: str) -> str:
    """
    Generate a password reset token for email verification.
    
    Args:
        username: The user requesting the reset
        
    Returns:
        The generated token
    """
    token = secrets.token_urlsafe(32)
    tokens = load_tokens()
    
    # Clean up expired tokens
    now = datetime.now()
    tokens = {
        k: v for k, v in tokens.items()
        if datetime.fromisoformat(v["expires"]) > now
    }
    
    # Store new token (just username, password will be set when user clicks link)
    tokens[token] = {
        "username": username,
        "expires": (now + timedelta(hours=TOKEN_EXPIRY_HOURS)).isoformat(),
        "created": now.isoformat()
    }
    
    save_tokens(tokens)
    return token


def verify_reset_token(token: str) -> Optional[dict]:
    """
    Verify a password reset token and return the associated data.
    
    Args:
        token: The token to verify
        
    Returns:
        Dict with username, password_hash, salt if valid, None otherwise
    """
    tokens = load_tokens()
    
    if token not in tokens:
        return None
    
    token_data = tokens[token]
    expires = datetime.fromisoformat(token_data["expires"])
    
    if datetime.now() > expires:
        # Token expired, remove it
        del tokens[token]
        save_tokens(tokens)
        return None
    
    return token_data


def consume_reset_token(token: str) -> Optional[dict]:
    """
    Verify and consume (delete) a password reset token.
    
    Args:
        token: The token to consume
        
    Returns:
        Dict with username, password_hash, salt if valid, None otherwise
    """
    tokens = load_tokens()
    
    if token not in tokens:
        return None
    
    token_data = tokens[token]
    expires = datetime.fromisoformat(token_data["expires"])
    
    if datetime.now() > expires:
        del tokens[token]
        save_tokens(tokens)
        return None
    
    # Remove the token (single use)
    del tokens[token]
    save_tokens(tokens)
    
    return token_data


def send_password_reset_email(
    to_email: str,
    username: str,
    reset_link: str
) -> bool:
    """
    Send a password reset verification email.
    
    Args:
        to_email: Recipient email address
        username: Username for personalization
        reset_link: The full URL for password reset verification
        
    Returns:
        True if email sent successfully, False otherwise
    """
    if not SMTP_PASSWORD:
        print("WARNING: SMTP password not configured, cannot send email")
        return False
    
    # Create message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Password Change Verification - Auroran Health Dashboard"
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to_email
    
    # Plain text version
    text_content = f"""
Hello {username},

You have requested to change your password for the Auroran Health Dashboard.

Please click the link below to verify and complete your password change:

{reset_link}

This link will expire in {TOKEN_EXPIRY_HOURS} hours.

If you did not request this password change, please ignore this email and your password will remain unchanged.

Best regards,
Auroran Health Dashboard Team
"""
    
    # HTML version
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }}
        .container {{ max-width: 600px; margin: 0 auto; background: #161b22; border-radius: 12px; padding: 30px; }}
        .header {{ text-align: center; margin-bottom: 30px; }}
        .header h1 {{ color: #58a6ff; font-size: 24px; }}
        .content {{ line-height: 1.6; }}
        .button {{ display: inline-block; background: #58a6ff; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: 600; margin: 20px 0; }}
        .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #30363d; color: #8b949e; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🦞 Auroran Health Dashboard</h1>
        </div>
        <div class="content">
            <p>Hello <strong>{username}</strong>,</p>
            <p>You have requested to change your password for the Auroran Health Dashboard.</p>
            <p>Please click the button below to verify and complete your password change:</p>
            <p style="text-align: center;">
                <a href="{reset_link}" class="button">Verify Password Change</a>
            </p>
            <p>Or copy and paste this link into your browser:</p>
            <p style="word-break: break-all; color: #58a6ff;">{reset_link}</p>
            <p>This link will expire in <strong>{TOKEN_EXPIRY_HOURS} hours</strong>.</p>
            <p>If you did not request this password change, please ignore this email and your password will remain unchanged.</p>
        </div>
        <div class="footer">
            <p>Best regards,<br>Auroran Health Dashboard Team</p>
        </div>
    </div>
</body>
</html>
"""
    
    msg.attach(MIMEText(text_content, "plain"))
    msg.attach(MIMEText(html_content, "html"))
    
    try:
        if SMTP_USE_SSL:
            # Port 465: Direct SSL/TLS connection
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            # Port 587: STARTTLS
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
        print(f"Password reset email sent to {to_email}")
        return True
    except Exception as e:
        print(f"ERROR: Failed to send email: {e}")
        return False

#!/usr/bin/env python3
"""
SMTP Configuration Test Script
Tests that SMTP settings are correct and can send email.

Usage: python test_smtp.py [recipient_email]
"""

import sys
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from config import (
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, 
    SMTP_FROM_EMAIL, SMTP_FROM_NAME
)


def test_smtp_config():
    """Display current SMTP configuration (hiding password)"""
    print("=" * 50)
    print("SMTP Configuration")
    print("=" * 50)
    print(f"  Host:       {SMTP_HOST}")
    print(f"  Port:       {SMTP_PORT}")
    print(f"  User:       {SMTP_USER}")
    print(f"  Password:   {'*' * len(SMTP_PASSWORD) if SMTP_PASSWORD else '(not set)'}")
    print(f"  From Email: {SMTP_FROM_EMAIL}")
    print(f"  From Name:  {SMTP_FROM_NAME}")
    print("=" * 50)
    
    if not SMTP_PASSWORD:
        print("\nERROR: SMTP password is not configured!")
        print("Please set smtp_password in smtp_config.json")
        return False
    
    return True


def test_smtp_connection():
    """Test SMTP server connection"""
    print("\nTesting SMTP connection...")
    
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            print(f"  Connected to {SMTP_HOST}:{SMTP_PORT}")
            
            server.starttls()
            print("  TLS encryption enabled")
            
            server.login(SMTP_USER, SMTP_PASSWORD)
            print("  Login successful")
            
        print("  Connection test PASSED")
        return True
        
    except smtplib.SMTPAuthenticationError as e:
        print(f"  Authentication FAILED: {e}")
        return False
    except smtplib.SMTPConnectError as e:
        print(f"  Connection FAILED: {e}")
        return False
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


def send_test_email(recipient: str):
    """Send a test email"""
    print(f"\nSending test email to {recipient}...")
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Test Email - Auroran Health Dashboard"
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    msg["To"] = recipient
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    text_content = f"""
Hello!

This is a test email from Auroran Health Dashboard.

Sent at: {timestamp}
From: {SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>

If you received this email, your SMTP configuration is working correctly!

Best regards,
Auroran Health Dashboard
"""
    
    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }}
        .container {{ max-width: 600px; margin: 0 auto; background: #161b22; border-radius: 12px; padding: 30px; }}
        .header {{ text-align: center; margin-bottom: 30px; }}
        .header h1 {{ color: #58a6ff; font-size: 24px; }}
        .success {{ background: #238636; color: white; padding: 15px; border-radius: 8px; text-align: center; margin: 20px 0; }}
        .details {{ background: #21262d; padding: 15px; border-radius: 8px; margin: 20px 0; }}
        .details p {{ margin: 5px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🦞 Auroran Health Dashboard</h1>
        </div>
        <div class="success">
            ✓ SMTP Configuration Test Successful!
        </div>
        <div class="details">
            <p><strong>Sent at:</strong> {timestamp}</p>
            <p><strong>From:</strong> {SMTP_FROM_NAME} &lt;{SMTP_FROM_EMAIL}&gt;</p>
            <p><strong>SMTP Host:</strong> {SMTP_HOST}:{SMTP_PORT}</p>
        </div>
        <p>If you received this email, your SMTP configuration is working correctly!</p>
    </div>
</body>
</html>
"""
    
    msg.attach(MIMEText(text_content, "plain"))
    msg.attach(MIMEText(html_content, "html"))
    
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        
        print(f"  Test email sent successfully to {recipient}")
        return True
        
    except Exception as e:
        print(f"  Failed to send email: {e}")
        return False


def main():
    print("\n🦞 Auroran Health Dashboard - SMTP Test\n")
    
    # Check configuration
    if not test_smtp_config():
        sys.exit(1)
    
    # Test connection
    if not test_smtp_connection():
        sys.exit(1)
    
    # Send test email if recipient provided
    if len(sys.argv) > 1:
        recipient = sys.argv[1]
        if not send_test_email(recipient):
            sys.exit(1)
        print("\n✓ All tests passed!")
    else:
        print("\n✓ Connection test passed!")
        print("\nTo send a test email, run:")
        print(f"  python {sys.argv[0]} your@email.com")


if __name__ == "__main__":
    main()

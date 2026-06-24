import os
import threading
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Define the scopes
SCOPES = ['https://www.googleapis.com/auth/drive']

# Thread-local storage for safe multi-threaded Google API client usage
thread_local = threading.local()

def authenticate():
    """Authenticates the user and returns credentials and service."""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=8080)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    service = build('drive', 'v3', credentials=creds)
    return creds, service

def get_service(creds):
    """Returns a thread-local service instance to ensure thread safety."""
    if not hasattr(thread_local, "service"):
        thread_local.service = build('drive', 'v3', credentials=creds)
    return thread_local.service

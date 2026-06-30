import sys
import os

# Add the project directory to the python path
sys.path.insert(0, os.path.dirname(__file__))

# Import the Flask application instance.
# cPanel Phusion Passenger expects the WSGI callable to be named 'application'.
from app import app as application

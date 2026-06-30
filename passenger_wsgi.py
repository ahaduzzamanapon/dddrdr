import os
import sys
import traceback

# Add project directory to python path
base_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, base_dir)

error_log = os.path.join(base_dir, "passenger_error.log")

try:
    # Import the Flask app instance.
    # cPanel Phusion Passenger expects the WSGI callable to be named 'application'.
    from app import app as application
    
    with open(error_log, "a") as f:
        f.write("Passenger WSGI loaded Flask application successfully!\n")
except Exception as e:
    with open(error_log, "a") as f:
        f.write(f"Passenger WSGI Import/Startup Error: {e}\n")
        f.write(traceback.format_exc() + "\n")
    raise

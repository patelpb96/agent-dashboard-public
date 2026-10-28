#!/bin/sh
# Starts the Agent Dashboard and opens it in your browser. Ctrl+C stops it.
exec python3 "$(dirname "$0")/server.py" --open

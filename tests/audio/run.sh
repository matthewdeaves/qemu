#!/bin/sh
# Build and run the offline audio tests: ./run.sh
set -e
cd "$(dirname "$0")"
python3 test_screamer_no_voice.py
echo "all audio tests passed"

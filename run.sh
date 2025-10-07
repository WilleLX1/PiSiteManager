cd /home/TheEye/github/PiSiteManager
source .venv/bin/activate
uvicorn manager:app --host 0.0.0.0 --port 8088 --reload

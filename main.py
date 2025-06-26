python\nfrom fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get(\"/\")\ndef read_root():\n return {\"status\": \"ok\", \"message\": \"LedWall portal online ✔\"}\n

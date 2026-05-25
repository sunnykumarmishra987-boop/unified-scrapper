# Tender Scraper — Railway Cron Deployment

## Project Structure
```
.
├── Dockerfile
├── requirements.txt
├── railway.toml
├── unified_Scrapper.py
└── .env.example
```

## Deploy Steps

### 1. Push to GitHub
```bash
git init
git add .
git commit -m "initial scraper deployment"
git remote add origin https://github.com/YOUR_USERNAME/tender-scraper.git
git push -u origin main
```

### 2. Create Railway Project
1. Go to https://railway.app → **New Project**
2. Choose **Deploy from GitHub repo** → select your repo
3. Railway auto-detects the `Dockerfile` and builds it

### 3. Set Environment Variables
In Railway → your service → **Variables**, add:
```
DATABASE_URL    = postgresql://user:pass@host:5432/dbname
OCR_URL         = https://your-ocr-service/predict
```

### 4. Configure as Cron Job
Railway reads `railway.toml` automatically. The default schedule is:
```
0 */6 * * *   →  every 6 hours
```

To change the schedule, edit `cronSchedule` in `railway.toml`:
```
"0 2 * * *"       →  daily at 2 AM UTC
"*/30 * * * *"    →  every 30 minutes
"0 6,12,18 * * *" →  3x per day at 6am, 12pm, 6pm UTC
```

### 5. Verify Deployment
- Railway → **Deployments** → watch the build logs
- After first successful build, go to **Cron** tab to see schedule & run history
- Click **Trigger Run** to test manually without waiting for the schedule

## Notes
- The scraper exits with code 0 on success. Railway marks the cron run as ✅
- `restartPolicyType = "never"` ensures Railway doesn't loop-restart on exit
- Playwright Chromium is baked into the Docker image (no separate install needed at runtime)
- The OCR service for CAPTCHA solving must be publicly reachable from Railway's network

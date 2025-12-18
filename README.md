# OII-NJ Legislature Proceedings Analysis 🏛️

A serverless AI pipeline that monitors New Jersey Legislature hearings, transcribes audio, and generates summaries using Amazon Bedrock.

## 🚀 Architecture
This system operates on a hybrid trigger model:
1. **Scheduled:** Amazon EventBridge runs daily checks for new hearings.
2. **On-Demand:** Users can request reports via an Amazon Bedrock Agent.
3. **Processing:** AWS Step Functions orchestrates downloading, transcribing, and summarizing.

## 📂 Project Structure
- `src/media_fetcher`: Selenium-based scraper to find audio links and download the media file from the NJ legislative websites
- `src/media_transcriber`: Transcribes audio files using Amazon Transcribe.
- `src/diarizer`: dairize the raw transcription generated with Amazon Transcribe. 
- `src/report-generator`: generate reports from the diarzed transcript 
- `src/bedrock_router`: Routes agent requests to the pipeline.

## 🛠️ Deployment (Manual)
Since this project does not use IaC, deployment is done via the AWS Console:
1. **Lambdas:** Create functions for Fetcher and Router using Python 3.11.
2. **Layers:** Attach `selenium` and `ffmpeg` layers to the Fetcher.
3. **Step Functions:** Import the workflow JSON from the `docs/` folder.
4. **EventBridge:** Configure the Cron schedule `0 8 ? * MON-FRI *`.

## 📦 Dependencies
See `requirements.txt` for the list of Python packages.
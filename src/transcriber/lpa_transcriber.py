import json
import boto3
import uuid
import os
from urllib.parse import urlparse

# Initialize the AWS Transcribe client
transcribe_client = boto3.client('transcribe')

def lambda_handler(event, context):
    print(f"Received event: {json.dumps(event)}")

    s3_uri = None
    s3_bucket = None
    s3_key = None

    # Default fallback name if parsing fails
    base_filename = "Legislative_Meeting"

    # --- 1. PARSE INPUT (Handle Step Function / Media Fetcher Output) ---
    try:
        # Check if input comes from the Media Fetcher (API Gateway style response)
        if 'body' in event:
            # The body is a stringified JSON, so we parse it
            body_data = event['body']
            if isinstance(body_data, str):
                body_data = json.loads(body_data)
            
            s3_uri = body_data.get('s3_uri')
            #Fetch the filename passed from Media Fetcher
            base_filename = body_data.get('base_filename', base_filename)
        
        # Check if input was passed directly as simple JSON (Manual test)
        elif 's3_uri' in event:
            s3_uri = event['s3_uri']
            base_filename = event.get('base_filename', base_filename)
        
        # --- 2. EXTRACT BUCKET AND KEY FROM URI ---
        if s3_uri:
            # Parse s3://bucket/key/file.mp3
            parsed = urlparse(s3_uri)
            s3_bucket = parsed.netloc
            s3_key = parsed.path.lstrip('/') # Remove leading slash
        
        # Validation
        if not s3_bucket or not s3_key:
            raise ValueError(f"Could not extract Bucket and Key from URI: {s3_uri}")

        print(f"[INFO] Extracted - Bucket: {s3_bucket}, Key: {s3_key}")

    except Exception as e:
        print(f"[ERROR] Input parsing failed: {e}")
        return {
            'statusCode': 400,
            'error': f"Failed to parse input. Expected s3_uri. Error: {str(e)}"
        }

    # --- 3. CONFIGURATION ---
    # Create a Job Name using the readable base filename
    short_id = str(uuid.uuid4())[:5]
    job_name = f"{base_filename}_Transcribe_{short_id}"
    
    # Output bucket (Default to the same bucket if env var not set)
    output_bucket = os.environ.get('OUTPUT_S3_BUCKET', s3_bucket)
    
    # --- 4. START TRANSCRIBE JOB ---
    try:
        print(f"Starting transcription job '{job_name}'")
        
        transcribe_client.start_transcription_job(
            TranscriptionJobName=job_name,
            LanguageCode='en-US',
            Media={'MediaFileUri': s3_uri},
            OutputBucketName=output_bucket,
            OutputKey=f"raw_transcripts/{job_name}.json",
            Settings={
                'ShowSpeakerLabels': True,
                'MaxSpeakerLabels': 15,
                'ChannelIdentification': True 
            }
        )
        
        # Construct the location where the transcript WILL be saved
        transcript_s3_key = f"raw_transcripts/{job_name}.json"
        transcript_s3_uri = f"s3://{output_bucket}/{transcript_s3_key}"

    except Exception as e:
        print(f"[ERROR] Transcribe job failed: {e}")
        raise e

    # --- 5. RETURN OUTPUT FOR NEXT STEP (DIARIZATION) ---
    # The next lambda in the Step Function needs these details
    return {
        "statusCode": 200,
        "job_name": job_name,
        "transcript_bucket": output_bucket,
        "transcript_key": transcript_s3_key,
        "transcript_s3_uri": transcript_s3_uri,
        "status": "IN_PROGRESS",
        "base_filename": base_filename
    }
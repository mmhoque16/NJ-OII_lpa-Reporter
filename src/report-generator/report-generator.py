import json
import boto3
import os
import re

# Set region explicitly
bedrock_runtime = boto3.client('bedrock-runtime', region_name='us-east-1')
s3_client = boto3.client('s3')

def parse_report_sections(report_text):
    """
    Parses the structured text from Bedrock into a Python dictionary.
    Assumes headers look like: 1. **Heading**:
    """
    sections = {}
    # Regex to find the numbered bold headers used in the prompt
    pattern = r'\d+\.\s+\*\*(.*?)\*\*:'
    
    split_text = re.split(pattern, report_text)
    
    # We skip index 0 (intro text) and iterate in pairs (Header, Body)
    for i in range(1, len(split_text), 2):
        header = split_text[i].strip()
        body = split_text[i+1].strip()
        clean_key = header.lower().replace(' ', '_')
        sections[clean_key] = body
        
    # Include full text as backup
    sections['full_raw_text'] = report_text
    return sections

def lambda_handler(event, context):
    print("Received event:", json.dumps(event))
    
    try:
        # --- 1. PARSE INPUT (From Step Function or Bedrock Agent) ---
        s3_bucket = event.get('diarized_bucket')
        diarized_key = event.get('diarized_txt_key')
        # Get the clean name passed from the very start
        base_filename = event.get('base_filename')
        
        if not s3_bucket: raise ValueError("Missing 'diarized_bucket'.")

       # Fallback if base_filename was somehow lost (e.g. manual run)
        if not base_filename:
            base_filename = os.path.basename(diarized_key).replace('_diarized.txt', '')

        # --- 2. Fetch Text ---
        obj = s3_client.get_object(Bucket=s3_bucket, Key=diarized_key)
        diarized_transcript = obj['Body'].read().decode('utf-8')

        # --- 3. GENERATE REPORT (BEDROCK) ---
        report_prompt = f"""
        Human: You are an expert legislative analyst. Based on the following full, diarized transcript of a hearing, generate a comprehensive report. The report must include the following four sections, each with a clear heading:
        1.  **Executive Summary**: A neutral, one-paragraph summary of the entire proceeding, including the main purpose and key events.
        2.  **Bills Discussed**: A bulleted list of all bill numbers mentioned (e.g., S-1234, A-5678) along with a brief, one-sentence description of each bill's purpose.
        3.  **Points of Conflict**: A bulleted list summarizing the main points of disagreement between different speakers or groups. For each point, briefly state the opposing views (e.g., "Ratepayer advocates argued for lower costs, while utility representatives warned about impacts to infrastructure investment.").
        4.  **Legislator Concerns**: A bulleted list summarizing the primary concerns or questions raised by the legislators (Senators, Assemblymembers) during the discussion.
        5.  **Final Outcome**: A concise summary of the outcome for each bill discussed, such as whether it was passed, held, or advanced for a second reading.
        6.  **Memorable Quote**: One single, impactful quote from the transcript that best captures the essence of the discussion.

        Full Transcript:
        <transcript>
        {diarized_transcript}
        </transcript>

        Assistant:
        """
        
        resp = bedrock_runtime.invoke_model(
            body=json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 4096, "messages": [{"role": "user", "content": report_prompt}]}),
            modelId='us.anthropic.claude-3-5-sonnet-20241022-v2:0'
        )
        final_report = json.loads(resp['body'].read().decode('utf-8'))['content'][0]['text']

        # --- 4. Save using BASE FILENAME ---
        # Format: final-reports/SEG_2025-06-12_Summary.txt
        txt_key = f"final-reports/{base_filename}_Summary.txt"
        json_key = f"final-reports/{base_filename}_Summary.json"
        
        s3_client.put_object(Bucket=s3_bucket, Key=txt_key, Body=final_report, ContentType='text/plain')
        s3_client.put_object(Bucket=s3_bucket, Key=json_key, Body=json.dumps(parse_report_sections(final_report), indent=2), ContentType='application/json')

        return {
            "statusCode": 200,
            "report_bucket": s3_bucket,
            "final_txt_uri": f"s3://{s3_bucket}/{txt_key}",
            "final_json_uri": f"s3://{s3_bucket}/{json_key}"
        }

    except Exception as e:
        print(f"Error: {e}")
        raise e
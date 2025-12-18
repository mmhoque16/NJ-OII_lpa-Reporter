import json
import boto3
import uuid
import os

# Initialize Client
sfn_client = boto3.client('stepfunctions')
STATE_MACHINE_ARN = os.environ.get('STATE_MACHINE_ARN')

def lambda_handler(event, context):
    print("--- 1. Incoming Event ---")
    print(json.dumps(event))
    
    # --- 1. DYNAMICALLY CAPTURE CONTEXT (The Mirror Strategy) ---
    # we capture exactly what Bedrock sent us.
    # This prevents "mismatch" errors.
    action_group = event.get('actionGroup', '') 
    api_path = event.get('apiPath', '')
    http_method = event.get('httpMethod', '')
    
      
    # --- 2. Parse Parameters ---
    params = {}
    
    # Check 'parameters' list (Standard)
    if 'parameters' in event:
        for p in event['parameters']:
            params[p['name']] = p['value']
            
    # Check 'requestBody' (Fallback)
    if not params and 'requestBody' in event:
        try:
            props = event['requestBody']['content']['application/json']['properties']
            for p in props:
                params[p['name']] = p['value']
        except:
            pass

    print(f"--- 2. Parsed Parameters: {params} ---")

    committee = params.get('committee_name')
    session = params.get('session')
    
    # Initialize Response Data
    response_data = {}
    status_code = 200

    # --- 3. Execute Logic ---
    if not committee or not session:
        print("[ERROR] Missing parameters")
        response_data = {"error": f"Missing parameters. Received: {params}"}
        status_code = 400
    elif not STATE_MACHINE_ARN:
        print("[ERROR] STATE_MACHINE_ARN not set")
        response_data = {"error": "Server configuration error (Missing ARN)"}
        status_code = 500
    else:
        try:
            execution_name = f"Bedrock-{str(uuid.uuid4())[:8]}"
            sfn_input = {
                "committee_name": committee,
                "session": session
            }
            
            sf_response = sfn_client.start_execution(
                stateMachineArn=STATE_MACHINE_ARN,
                name=execution_name,
                input=json.dumps(sfn_input)
            )
            
            print(f"--- 3. Step Function Started: {sf_response['executionArn']} ---")
            
            response_data = {
                "message": f"Success! Pipeline started for {committee}.",
                "job_id": sf_response['executionArn'],
                "status": "IN_PROGRESS"
            }
        except Exception as e:
            print(f"[ERROR] Step Function failed: {e}")
            response_data = {"error": str(e)}
            status_code = 500

    # --- 4. Build Response (The Universal Structure) ---
    # We construct the response object based on what we received.
    
    response_body = {
        "application/json": {
            "body": json.dumps(response_data) # Must be a string
        }
    }

    action_response = {
        "actionGroup": action_group,
        "httpStatusCode": status_code,
        "responseBody": response_body
    }

    # CRITICAL: Only return the fields Bedrock actually sent us.
    # If we return 'apiPath' when Bedrock expects 'function', it crashes.
    if api_path:
        action_response["apiPath"] = api_path
        action_response["httpMethod"] = http_method
    elif function_name:
        action_response["function"] = function_name

    final_response = {
        "messageVersion": "1.0",
        "response": action_response
    }
    
    print("--- 4. Final Response to Bedrock ---")
    print(json.dumps(final_response))
    
    return final_response
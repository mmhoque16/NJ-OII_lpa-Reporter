# lambda_function.py

import json, os, re
from typing import List, Dict, Any, Optional 
import boto3
from botocore.config import Config
import botocore
import itertools
# =========================
# Hard-coded Configuration
# =========================
BEDROCK_REGION = "us-east-1"  # <- set your Bedrock region here
MODEL_ID = "us.anthropic.claude-3-5-sonnet-20241022-v2:0"  #  make sure your account has access in this region
OUTPUT_PREFIX = "diarized-transcription/"  # <- S3 prefix for outputs (no spaces recommended)

# Long-ish timeouts in case of big requests
_bedrock_cfg = Config(read_timeout=1100, connect_timeout=60, retries={"max_attempts": 0})

bedrock_runtime = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION, config=_bedrock_cfg)
s3_client = boto3.client("s3")


# =========================
# Diarization Helpers
# =========================

def _build_ranges(transcribe: Dict[str, Any]) -> List[tuple]:
    """
    Builds [(start, end, speaker_label)] from the main results.speaker_labels.
    Robustly handles cases where speaker_labels is a list or a dict.
    """
    results = transcribe.get("results", {})
    if "speaker_labels" not in results:
        # It's possible the transcript relies on embedded labels in items, 
        # so we warn but don't raise an error if missing.
        print("[WARN] 'speaker_labels' section missing from results.")
        return []

    labels = results["speaker_labels"]
    segments = []
    
    # --- FIX: Handle speaker_labels as List or Dict ---
    if isinstance(labels, list):
        # If it's a list, assume it's the list of segments directly
        segments = labels
    elif isinstance(labels, dict):
        # Standard format
        segments = labels.get("segments", [])
    # --------------------------------------------------

    ranges = []
    for i, s in enumerate(segments):
        if not isinstance(s, dict):
            continue

        st = float(s.get("start_time", 0))
        en = float(s.get("end_time", 0))
        label = s.get("speaker_label")
        
        if label is not None:
            ranges.append((st, en, label))
            
    return ranges

def _speaker_for_time(ranges: List[tuple], t: float, last_label: str = None) -> str:
    for st, en, lab in ranges:
        if st <= t < en:
            return lab
    return last_label

def reconstruct_utterances_with_timestamps(transcribe: Dict[str, Any], gap_seconds: float = 1.2) -> List[Dict[str, Any]]:
    """
    Reconstructs speaker utterances.
    Fixes:
    1. Robustly handles 'list' vs 'dict' structures for channels.
    2. DEDUPLICATES identical words occurring on multiple channels (fixes "Well Well" issue).
    """
    results = transcribe.get("results", {})
    if not isinstance(results, dict):
        raise TypeError(f"The 'results' key should be a dict, got {type(results)}")

    items = []
    
    # --- 1. Harvest items from all channels ---
    if "channel_labels" in results:
        print("[INFO] Channel identification detected. Merging items from all channels.")
        c_labels = results["channel_labels"]
        channels = []
        
        # Handle list vs dict
        if isinstance(c_labels, list):
            channels = c_labels
        elif isinstance(c_labels, dict):
            channels = c_labels.get("channels", [])
            
        all_channel_items = []
        for channel in channels:
            if isinstance(channel, dict):
                # Standard: items is a key inside the channel dict
                c_items = channel.get("items", [])
                if isinstance(c_items, list):
                    all_channel_items.extend(c_items)
            elif isinstance(channel, list):
                # Non-standard: channel is the list of items itself
                all_channel_items.extend(channel)
        
        # Filter valid items
        items = [it for it in all_channel_items if isinstance(it, dict)]
        
        # Sort by time so duplicates appear next to each other
        items = sorted(items, key=lambda x: float(x.get("start_time", "inf")))

    elif "items" in results:
        print("[INFO] Single channel transcript detected.")
        raw = results["items"]
        if isinstance(raw, list):
            items = [it for it in raw if isinstance(it, dict)]
    else:
        raise ValueError("Transcript missing 'items' or 'channel_labels'.")

    # --- 2. Deduplicate Items (The Fix for "Double Words") ---
    # If channel 0 and channel 1 have the exact same text at the exact same time, drop one.
    unique_items = []
    if items:
        unique_items.append(items[0])
        for i in range(1, len(items)):
            prev = unique_items[-1]
            curr = items[i]
            
            try:
                # Get start times
                t1 = float(prev.get("start_time", -1.0))
                t2 = float(curr.get("start_time", -1.0))
                
                # Get content
                c1 = prev.get("alternatives", [{}])[0].get("content", "")
                c2 = curr.get("alternatives", [{}])[0].get("content", "")
                
                # If distinct enough in time or content, keep it.
                # (Use a tiny epsilon for float comparison, e.g. 0.001s)
                if abs(t2 - t1) > 0.001 or c1 != c2:
                    unique_items.append(curr)
            except Exception:
                # If data is missing/malformed, keep to be safe
                unique_items.append(curr)
                
    items = unique_items

    # --- 3. Build Speaker Ranges ---
    ranges = _build_ranges(transcribe)
        
    utterances = []
    cur = {"speaker_label": None, "start_time": None, "end_time": None, "text_parts": []}
    last_word_end = None

    def _flush():
        nonlocal cur
        if cur.get("speaker_label") and cur.get("text_parts"):
            utterances.append({
                "speaker_label": cur["speaker_label"],
                "start_time": cur["start_time"],
                "end_time": cur["end_time"],
                "text": "".join(cur["text_parts"]).strip()
            })
        cur = {"speaker_label": None, "start_time": None, "end_time": None, "text_parts": []}

    # --- 4. Reconstruct Text ---
    for it in items:
        if not isinstance(it, dict): continue

        alts = it.get("alternatives", [])
        if not isinstance(alts, list) or not alts: continue
        content = alts[0].get("content", "") if isinstance(alts[0], dict) else ""

        if it.get("type") == "pronunciation":
            st = float(it.get("start_time", 0))
            en = float(it.get("end_time", 0))
            
            # Use embedded label if present (some custom formats), else look up in ranges
            label = it.get("speaker_label")
            if not label:
                label = _speaker_for_time(ranges, st, cur.get("speaker_label"))
            
            # If speaker changed OR gap is too large, flush the buffer
            if (label != cur["speaker_label"]) or (last_word_end is not None and st - last_word_end > gap_seconds):
                _flush()
                cur["speaker_label"] = label
                cur["start_time"] = st
            
            # Add space if needed
            if cur["text_parts"] and not cur["text_parts"][-1].endswith((" ", "\n")):
                cur["text_parts"].append(" ")
            
            cur["text_parts"].append(content)
            cur["end_time"] = en
            last_word_end = en
        else:
            # Punctuation
            cur["text_parts"].append(content)
    
    _flush()
    return utterances
#
def coalesce_utterances(utterances: List[Dict[str, Any]], gap_seconds: Optional[float] = None) -> List[Dict[str, Any]]:

    """
    Merge consecutive utterances by the same speaker.
    If gap_seconds is provided, only merge when the time gap between blocks <= gap_seconds.
    """
    if not utterances:
        return utterances
    merged = [dict(utterances[0])]
    for u in utterances[1:]:
        last = merged[-1]
        same_speaker = (u["speaker_label"] == last["speaker_label"])
        gap_ok = True if gap_seconds is None else (u["start_time"] - last["end_time"] <= gap_seconds)
        if same_speaker and gap_ok:
            # Append text with proper spacing.
            if last["text"] and not last["text"].endswith((" ", "\n")):
                last["text"] += " "
            last["text"] += u["text"]
            last["end_time"] = u["end_time"]  # extend the time window
        else:
            merged.append(dict(u))
    return merged

def build_raw_for_bedrock(utterances: List[Dict[str, Any]]) -> str:
    """Compact label-only view for LLM context (one line per utterance)."""
    return "\n".join(f"[{u['speaker_label']}] {u['text']}" for u in utterances)

def format_diarized_lines_no_ts(
    utterances: List[Dict[str, Any]],
    speaker_map: Dict[str, str],
    blank_lines: int = 1,   # <- number of blank lines between speakers
) -> str:
    """
    Human-readable transcript WITHOUT timestamps:
    **Speaker Name:** text

    `blank_lines` controls how many empty lines are inserted between speaker blocks.
    For example:
      blank_lines=1 -> one empty line between blocks (i.e., '\n\n')
      blank_lines=2 -> two empty lines between blocks (i.e., '\n\n\n')
    """
    lines: List[str] = []
    for u in utterances:
        name = speaker_map.get(u["speaker_label"], "[UNKNOWN SPEAKER]")
        lines.append(f"**{name}:** {u['text']}")

    # in the join separator. (e.g., 2 blank lines => '\n' * 3)
    separator = "\n" * (blank_lines + 1)
    return separator.join(lines) + "\n"


def utterances_to_jsonl(utterances: List[Dict[str, Any]], speaker_map: Dict[str, str]) -> str:
    """Machine-readable JSONL for analytics (keeps timings)."""
    rows = []
    for u in utterances:
        rows.append(json.dumps({
            "start_time": u["start_time"],
            "end_time": u["end_time"],
            "speaker_label": u["speaker_label"],
            "speaker_name": speaker_map.get(u["speaker_label"], None),
            "text": u["text"]
        }, ensure_ascii=False))
    return "\n".join(rows)


# =========================
# Bedrock Speaker Mapping (Refined Prompt)
# =========================

def get_speaker_map_via_bedrock(raw_for_bedrock: str) -> Dict[str, str]:
    """
    Ask the model for a strict JSON mapping { "spk_#": "Full Name or Title" }.
    If unknown, model should return "[UNKNOWN SPEAKER]".
    """
    prompt = (
        "You are an expert legislative-hearing analyst. Your task is to map generic diarization labels "
        "(e.g., [spk_0], [spk_1]) to actual speaker names or official titles found in the transcript.\n\n"
        "INSTRUCTIONS:\n"
        "1) Read the entire transcript to understand the context and identify speakers.\n"
        "2) Use ONLY names/titles explicitly supported by the transcript: self-introductions, roll call, "
        "   direct address (e.g., \"Senator Smith\", \"Madam Chair\"), or when a name is called and another "
        "   label immediately responds.\n"
        "3) Identify speaker names from context when they introduce themselves (e.g., My name is Jane Doe, or This is Dave Cole).\n"
        "4) Pay close attention when one speaker calls on another. The dialogue immediately following that call should be attributed to the person who was called upon.\n"
        "5) Prefer the most specific formal version (e.g., \"Chairwoman Cruz Perez\" over \"Chair\").\n"
        "6) If there is not enough evidence, map to \"[UNKNOWN SPEAKER]\"—do not guess.\n"
        "7) Keep the mapping consistent: the same person must map to the same label for the entire transcript.\n"
        "8) Return ONLY a single JSON object with keys present in the input; no commentary or Markdown.\n\n"
        "INPUT (each line is an utterance):\n"
        f"{raw_for_bedrock}\n\n"
        "OUTPUT FORMAT EXAMPLE:\n"
        "{\n"
        "  \"spk_0\": \"Chairwoman Cruz Perez\",\n"
        "  \"spk_1\": \"Senator Pennacchio\",\n"
        "  \"spk_2\": \"[UNKNOWN SPEAKER]\"\n"
        "}\n"
    )

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "temperature": 0,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}]
    })

    try:
        resp = bedrock_runtime.invoke_model(
            modelId=MODEL_ID,
            accept="application/json",
            contentType="application/json",
            body=body
        )
        data = json.loads(resp["body"].read())
        text = data["content"][0]["text"]

        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            print("[WARN] No JSON object found in model output.")
            return {}
        return json.loads(m.group(0))
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        msg = e.response.get("Error", {}).get("Message")
        print(f"[ERROR] Bedrock ClientError {code}: {msg}")
        return {}
    except Exception as e:
        print(f"[ERROR] Bedrock mapping failed: {e}")
        return {}


# =========================
# Lambda Handler (Step Function Optimized)
# =========================

def lambda_handler(event, context):
    """
    1) Parse Input from Step Function
    2) Load Amazon Transcribe JSON from S3
    3) Reconstruct utterances
    4) Ask Bedrock for label→name mapping
    5) Save diarized files to S3 (Using passed-through base_filename)
    6) Return simple JSON for the next Step
    """
    print("Received event:", json.dumps(event))

    try:
        # --- 1. Parse Inputs (Step Function Logic) ---
        # Initialize variables
        s3_bucket = None
        transcript_key = None
        base_filename = "Legislative_Meeting_Output" # Default fallback

        # Check if input is nested in 'body'
        if 'body' in event and isinstance(event['body'], str):
             try:
                 body_data = json.loads(event['body'])
                 s3_bucket = body_data.get('transcript_bucket') or body_data.get('s3_bucket')
                 transcript_key = body_data.get('transcript_key') or body_data.get('s3_key')
                 # GRAB THE NAME FROM PREVIOUS STEP
                 base_filename = body_data.get('base_filename', base_filename)
             except:
                 pass
        
        # If not found in body, check top level
        if not s3_bucket:
            s3_bucket = event.get('transcript_bucket')
            transcript_key = event.get('transcript_key')
            # GRAB THE NAME FROM PREVIOUS STEP
            base_filename = event.get('base_filename', base_filename)

        # Validation
        if not s3_bucket or not transcript_key:
            # Fallback for testing: Check if using the old Bedrock Agent format
            if 'requestBody' in event:
                 print("[INFO] Detected Bedrock Agent format, attempting fallback parsing...")
                 properties = event["requestBody"]["content"]["application/json"]["properties"]
                 s3_bucket = next((p["value"] for p in properties if p.get("name") == "s3_bucket"), None)
                 transcript_key = next((p["value"] for p in properties if p.get("name") == "transcript_key"), None)
            
            if not s3_bucket or not transcript_key:
                raise ValueError(f"Missing required inputs 'transcript_bucket' or 'transcript_key'. Received: {event.keys()}")

        # ---- Load Transcribe JSON from S3 ----
        print(f"Fetching raw transcript from s3://{s3_bucket}/{transcript_key}")
        obj = s3_client.get_object(Bucket=s3_bucket, Key=transcript_key)
        transcript_data = json.loads(obj["Body"].read().decode("utf-8"))

        # ---- Rebuild utterances with timestamps & labels ----
        utterances = reconstruct_utterances_with_timestamps(transcript_data)
        if not utterances:
            raise ValueError("Could not reconstruct utterances from the provided file.")

        # ---- Prepare compact label-only view for the LLM ----
        raw_for_bedrock = build_raw_for_bedrock(utterances)

        # ---- Ask Bedrock for spk_# -> full name mapping ----
        speaker_map = get_speaker_map_via_bedrock(raw_for_bedrock)

        # ---- Coalesce and Format ----
        coalesced_for_txt = coalesce_utterances(utterances, gap_seconds=None)
        
        # Format Text (Human Readable)
        final_txt = format_diarized_lines_no_ts(coalesced_for_txt, speaker_map, blank_lines=2)
        
        # Format JSONL (Machine Readable)
        final_jsonl = utterances_to_jsonl(utterances, speaker_map)

        # ---- Write outputs to S3 (USING BASE FILENAME) ----
        # Use the clean base filename passed from the previous lambda
        # Example: diarized-transcription/SEG_2025-06-12_diarized.txt
        txt_key = f"{OUTPUT_PREFIX}{base_filename}_diarized.txt"
        jsonl_key = f"{OUTPUT_PREFIX}{base_filename}_diarized.jsonl"

        s3_client.put_object(Bucket=s3_bucket, Key=txt_key, Body=final_txt, ContentType="text/plain")
        s3_client.put_object(Bucket=s3_bucket, Key=jsonl_key, Body=final_jsonl, ContentType="application/jsonl")

        print(f"[SUCCESS] Diarized text saved to: s3://{s3_bucket}/{txt_key}")

        # ---- RETURN SIMPLE JSON FOR NEXT STEP ----
        # This is what the ReportGenerator will receive
        return {
            "statusCode": 200,
            "diarized_bucket": s3_bucket,
            "diarized_txt_key": txt_key,
            "diarized_jsonl_key": jsonl_key,
            "diarized_text_preview": final_txt[:1000], # Optional: Preview for logs
            # PASS IT FORWARD TO THE FINAL REPORT LAMBDA
            "base_filename": base_filename
        }

    except Exception as e:
        print(f"[ERROR] An exception occurred: {e}")
        # Raise exception so Step Functions marks this step as Failed
        raise e
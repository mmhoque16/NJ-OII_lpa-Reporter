# Manual Deployment Guide 🛠️

`This document outlines the step-by-step process to manually deploy the NJ Legislature AI Reporter infrastructure on AWS. Since this project does not use Infrastructure as Code (IaC), these instructions serve as the source of truth for configuration settings.`

## 1. Prerequisites
- **AWS Account** with administrative access.
- **Python 3.11** installed locally.
- **Chrome Driver / FFmpeg Layers** (Required for the Scraper Lambda).

---

## 2. Storage (Amazon S3)
1. **Create Bucket:**
   - Name: `nj-legislature-data-prod` (or unique variant)
   - Region: `us-east-1` (Recommended for Bedrock availability)
2. **Create Folders:**
   - `/audio`
   - `/transcripts`
   - `/reports`

---

## 3. Compute (AWS Lambda)

### A. Media Fetcher Lambda (`LPA-MediaFetcher`)
*Responsible for scraping the NJ Leg website and downloading audio.*

- **Runtime:** Python 3.11
- **Timeout:** **15 Minutes** (Critical: 900 seconds) - *Standard 3s timeout will fail.*
- **Memory:** 1024 MB (Recommended for Selenium)
- **Environment Variables:**
  - `S3_BUCKET`: `nj-legislature-data-prod`
- **Layers Required:**
  - `selenium-layer` (Ensure compatible with Python 3.11)
  - `ffmpeg-layer`
- **Permissions (IAM Role):**
  - `s3:PutObject` (on bucket)
  - `s3:GetObject` (on bucket)
  - `s3:ListBucket`

### B. Bedrock Router Lambda (`LPA-Router`)
*Connects the Bedrock Agent to the Step Function.*

- **Runtime:** Python 3.11
- **Timeout:** 30 Seconds
- **Permissions (IAM Role):**
  - `states:StartExecution` (Access to Step Functions)

---

## 4. Orchestration (AWS Step Functions)

**State Machine Name:** `LegislativePipeline`

1. Create a Standard State Machine.
2. Paste the following JSON definition (Replace `<YOUR_ACCOUNT_ID>` and `<REGION>` with your actual details):

{
  "Comment": "Legislative Media Processing Pipeline with Transcribe Polling Loop",
  "StartAt": "MediaFetcher",
  "States": {
    "MediaFetcher": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:<REGION>:<YOUR_ACCOUNT_ID>:function:media-fetcher",
      "Next": "TranscriptGenerator",
      "Retry": [
        {
          "ErrorEquals": [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException"
          ],
          "IntervalSeconds": 2,
          "MaxAttempts": 6,
          "BackoffRate": 2
        }
      ]
    },
    "TranscriptGenerator": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:<REGION>:<YOUR_ACCOUNT_ID>:function:LPA-Generating-Raw-Transcription",
      "Next": "WaitForTranscript",
      "InputPath": "$",
      "ResultPath": "$",
      "Retry": [
        {
          "ErrorEquals": [
            "States.Timeout"
          ],
          "IntervalSeconds": 30,
          "MaxAttempts": 0
        }
      ]
    },
    "WaitForTranscript": {
      "Type": "Wait",
      "Seconds": 60,
      "Next": "GetJobStatus"
    },
    "GetJobStatus": {
      "Type": "Task",
      "Resource": "arn:aws:states:::aws-sdk:transcribe:getTranscriptionJob",
      "Parameters": {
        "TranscriptionJobName.$": "$.job_name"
      },
      "ResultPath": "$.transcribe_status",
      "Next": "CheckStatusChoice"
    },
    "CheckStatusChoice": {
      "Type": "Choice",
      "Choices": [
        {
          "Variable": "$.transcribe_status.TranscriptionJob.TranscriptionJobStatus",
          "StringEquals": "COMPLETED",
          "Next": "DiarizingLambda"
        },
        {
          "Variable": "$.transcribe_status.TranscriptionJob.TranscriptionJobStatus",
          "StringEquals": "FAILED",
          "Next": "JobFailed"
        }
      ],
      "Default": "WaitForTranscript"
    },
    "DiarizingLambda": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:<REGION>:<YOUR_ACCOUNT_ID>:function:LPA-DiarizingTranscript",
      "Next": "ReportGenerator"
    },
    "ReportGenerator": {
      "Type": "Task",
      "Resource": "arn:aws:lambda:<REGION>:<YOUR_ACCOUNT_ID>:function:LPA-Report-Generation",
      "End": true
    },
    "JobFailed": {
      "Type": "Fail",
      "Cause": "AWS Transcribe Job Failed",
      "Error": "TranscribeJobFailed"
    }
  }
}
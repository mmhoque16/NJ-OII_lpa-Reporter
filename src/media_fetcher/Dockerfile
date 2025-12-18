# Base AWS Lambda image (Python 3.11)
FROM public.ecr.aws/lambda/python:3.11

# 1. Install system dependencies (Changed dnf to yum)
RUN yum update -y && \
    yum install -y \
    atk cups-libs gtk3 libXcomposite alsa-lib \
    libXcursor libXdamage libXext libXi libXrandr libXScrnSaver \
    libXtst pango at-spi2-atk libXt xorg-x11-server-Xvfb \
    xorg-x11-xauth dbus-glib dbus-glib-devel nss mesa-libgbm \
    jq unzip curl xz tar && \
    yum clean all

# 2. Install Chrome (Version 121 stable for Linux)
# We download directly to /opt/chrome
RUN curl -Lo "/tmp/chrome-linux64.zip" "https://storage.googleapis.com/chrome-for-testing-public/121.0.6167.85/linux64/chrome-linux64.zip" && \
    unzip "/tmp/chrome-linux64.zip" -d "/opt/" && \
    mv /opt/chrome-linux64 /opt/chrome && \
    chmod +x /opt/chrome/chrome && \
    rm "/tmp/chrome-linux64.zip"

# 3. Install ChromeDriver (Matching Version 121)
RUN curl -Lo "/tmp/chromedriver-linux64.zip" "https://storage.googleapis.com/chrome-for-testing-public/121.0.6167.85/linux64/chromedriver-linux64.zip" && \
    unzip "/tmp/chromedriver-linux64.zip" -d "/opt/" && \
    mv /opt/chromedriver-linux64 /opt/chromedriver && \
    chmod +x /opt/chromedriver/chromedriver && \
    rm "/tmp/chromedriver-linux64.zip"

# 4. Install FFmpeg & FFprobe (For yt-dlp)
RUN curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o /tmp/ffmpeg.tar.xz && \
    xz -d /tmp/ffmpeg.tar.xz && \
    tar -xf /tmp/ffmpeg.tar -C /tmp && \
    mv /tmp/ffmpeg-*/ffmpeg /usr/local/bin/ && \
    mv /tmp/ffmpeg-*/ffprobe /usr/local/bin/ && \
    chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe && \
    rm -rf /tmp/ffmpeg*

# 5. Set Environment Variables so your script finds them
# These paths are correctly pointed to the executable files
ENV CHROME_BIN="/opt/chrome/chrome"
ENV CHROMEDRIVER="/opt/chromedriver/chromedriver"

# 6. Install Python dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt --target "${LAMBDA_TASK_ROOT}"

# 7. Copy Main Application Code
COPY main.py ${LAMBDA_TASK_ROOT}/

# 8. Lambda Entry Point
CMD [ "main.lambda_handler" ]
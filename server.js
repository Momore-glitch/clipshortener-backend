const express = require("express");
const multer = require("multer");
const cors = require("cors");

const app = express();

app.use(cors());

app.use("/outputs", express.static("outputs"));

// store uploaded files temporarily
const upload = multer({ dest: "uploads/" });

// test route
app.get("/", (req, res) => {
  res.json({ ok: true, service: "clipshortener-api" });
});

// upload route
const fs = require("fs");
const { exec } = require("child_process");

const ytDlp = require("yt-dlp-exec");
const path = require("path");

async function downloadYouTube(url) {
    const output = path.join("uploads", "youtube_%(id)s.%(ext)s");

    await ytDlp(url, {
    output,
    format: "bv*+ba/b",
    mergeOutputFormat: "mp4",
    noWarnings: true,
    noCheckCertificates: true
});

    const files = fs.readdirSync("uploads")
    .filter(f => f.startsWith("youtube_"));

if (files.length === 0) {
    throw new Error("No downloaded file found.");
}

return path.join("uploads", files[0]);
}

app.post("/split", upload.single("video"), (req, res) => {

const videoUrl = req.body.videoUrl;

if (videoUrl) {
    console.log("YouTube URL received:", videoUrl);
}

if (videoUrl) {
  return res.json({
    message: "URL received",
    url: videoUrl
  });
}
  const videoUrl = req.body.videoUrl;

if (!req.file && !videoUrl) {
  return res.status(400).json({
    error: "No video uploaded or YouTube link provided"
  });
}

let inputPath;

if (videoUrl) {

    try {
        inputPath = await downloadYouTube(videoUrl);
        console.log("Downloaded to:", inputPath);
    } catch (err) {
        console.error(err);
        return res.status(500).json({
            error: "Failed to download YouTube video"
        });
    }

} else {
  inputPath = req.file.path;
}

  const inputPath = req.file.path;
  const outputDir = "outputs";

  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir);
  }

  const command = `ffmpeg -i ${inputPath} -c copy -map 0 -f segment -segment_time 30 -reset_timestamps 1 ${outputDir}/clip_%03d.mp4`;

  exec(command, (err, stdout, stderr) => {

    if (err) {
        console.error("FFmpeg Error:", err);
        console.error("STDERR:", stderr);
        console.error("STDOUT:", stdout);

        return res.status(500).json({
            error: "Processing failed",
            details: stderr || err.message
        });
    }

    const files = fs.readdirSync(outputDir).filter(f => f.endsWith(".mp4"));


    const baseUrl = req.protocol + "://" + req.get("host");

const clipUrls = files.map(f => baseUrl + "/outputs/" + f);

res.json({
  message: "Clips created",
  clips: clipUrls
});

const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
  console.log("Server running on port " + PORT);
});

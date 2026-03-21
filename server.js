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

app.post("/split", upload.single("video"), (req, res) => {
  if (!req.file) {
    return res.status(400).json({ error: "No file uploaded" });
  }

  const inputPath = req.file.path;
  const outputDir = "outputs";

  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir);
  }

  const command = `ffmpeg -i ${inputPath} -c copy -map 0 -f segment -segment_time 30 -reset_timestamps 1 ${outputDir}/clip_%03d.mp4`;

  exec(command, (err) => {
    if (err) {
      console.error(err);
      return res.status(500).json({ error: "Processing failed" });
    }

    const files = fs.readdirSync(outputDir).filter(f => f.endsWith(".mp4"));


    const baseUrl = req.protocol + "://" + req.get("host");

const clipUrls = files.map(f => baseUrl + "/outputs/" + f);

res.json({
  message: "Clips created",
  clips: clipUrls
});
    
  res.json({
    message: "Upload successful",
    filename: req.file.filename
  });
});

const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
  console.log("Server running on port " + PORT);
});

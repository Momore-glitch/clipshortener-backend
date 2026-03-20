const express = require("express");
const multer = require("multer");
const cors = require("cors");

const app = express();

app.use(cors());

// store uploaded files temporarily
const upload = multer({ dest: "uploads/" });

// test route
app.get("/", (req, res) => {
  res.json({ ok: true, service: "clipshortener-api" });
});

// upload route
app.post("/upload", upload.single("video"), (req, res) => {
  if (!req.file) {
    return res.status(400).json({ error: "No file uploaded" });
  }

  res.json({
    message: "Upload successful",
    filename: req.file.filename
  });
});

const PORT = process.env.PORT || 3000;

app.listen(PORT, () => {
  console.log("Server running on port " + PORT);
});

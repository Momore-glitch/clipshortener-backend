const express = require("express")
const multer = require("multer")
const cors = require("cors")
const fs = require("fs")
const { exec } = require("child_process")

const app = express()

app.use(cors())

const upload = multer({ dest: "uploads/" })

app.post("/split", upload.single("video"), (req,res)=>{

const file=req.file.path

const output="clips"

if(!fs.existsSync(output)) fs.mkdirSync(output)

const command=`ffmpeg -i ${file} -c copy -map 0 -segment_time 30 -f segment ${output}/clip_%03d.mp4`

exec(command,(err)=>{

if(err){
return res.status(500).send("processing failed")
}

res.send("done")

})

})

app.listen(3000,()=>console.log("server running"))

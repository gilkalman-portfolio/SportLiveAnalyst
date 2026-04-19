@echo off
cd /d "C:\Users\USER\PycharmProjects\SportLiveAnalyst"
"C:\Users\USER\AppData\Local\Programs\Python\Python314\python.exe" -m liveanalyst.worker >> logs\worker.log 2>&1

@echo off
rem ZeroReplica_LLM_8090 task entry.
rem Reads active_model.json (single source of truth for WHICH model) and models.json (single
rem source of truth for WHERE it lives, which llama-server build, which frozen parameters and
rem which port) and starts the server. Started via Task Scheduler so the process lives OUTSIDE
rem the WorkBuddy sandbox job (sandbox kills spawned children when the tool call ends - verified
rem 2026-09-21).
rem This file used to carry one `if "%ACTIVE_KEY%"==... set MODEL_PATH=...` line per GGUF plus a
rem hard-coded b11065 path and the frozen parameters, so adding a model meant editing the task
rem entry. It now resolves everything from the registry, which is what makes the lifecycle layer
rem model-agnostic.
rem ASCII-only: PS 5.1 misreads non-ASCII literals in BOM-less bat files.
setlocal
for %%I in ("%~dp0..\..") do set "ROOT=%%~fI"
title AI Girlfriend LLM 8090 http://127.0.0.1:8090
if not exist "%ROOT%\runtime\llm\logs" mkdir "%ROOT%\runtime\llm\logs"
set "LOOKUP=$active=(ConvertFrom-Json (Get-Content -Raw -Encoding UTF8 -LiteralPath '%ROOT%\runtime\llm\active_model.json')).active_model; $reg=(ConvertFrom-Json (Get-Content -Raw -Encoding UTF8 -LiteralPath '%ROOT%\runtime\llm\models.json')); $row=$reg.models.$active; if(-not $row){'active=MISSING'}else{'active='+$active; 'MODEL_PATH='+$row.path; 'LLAMA_EXE='+$reg.llama_server; 'FROZEN='+$reg.launch_params_frozen; 'HOST='+$reg.listen.host; 'PORT='+$reg.listen.port}"

set "active="
set "MODEL_PATH="
set "LLAMA_EXE="
set "FROZEN="
set "HOST="
set "PORT="
for /f "usebackq tokens=1,* delims==" %%i in (`powershell -NoProfile -Command "%LOOKUP%"`) do set "%%i=%%j"

if "%active%"=="MISSING" (
  echo UNKNOWN_ACTIVE_MODEL=no-row-in-models.json >> "%ROOT%\runtime\llm\logs\task_err.log"
  exit /b 2
)
if "%MODEL_PATH%"=="" (
  echo UNKNOWN_ACTIVE_MODEL=%active% >> "%ROOT%\runtime\llm\logs\task_err.log"
  exit /b 2
)
if "%LLAMA_EXE%"=="" (
  echo LLAMA_SERVER_PATH_UNDECLARED models.json llama_server is empty >> "%ROOT%\runtime\llm\logs\task_err.log"
  exit /b 3
)
if not exist "%MODEL_PATH%" (
  echo MODEL_FILE_MISSING=%MODEL_PATH% >> "%ROOT%\runtime\llm\logs\task_err.log"
  exit /b 4
)
if "%PORT%"=="" set PORT=8090
if "%HOST%"=="" set HOST=0.0.0.0

echo [%date% %time%] task start key=%active% exe=%LLAMA_EXE% >> "%ROOT%\runtime\llm\logs\task_run.log"
"%LLAMA_EXE%" %FROZEN% --host %HOST% --port %PORT% -m "%MODEL_PATH%" --log-file "%ROOT%\runtime\llm\logs\llama_8090_%active%.log"
echo [%date% %time%] task exit code=%errorlevel% >> "%ROOT%\runtime\llm\logs\task_run.log"

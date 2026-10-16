# Edit YOUR deployment; dot-source this file in your startup PowerShell session.
$env:SPEECH_TO_SPEECH_URL='http://127.0.0.1:8765'
$env:RTC_ICE_SERVERS='[{"urls":"stun:stun.l.google.com:19302"}]'
# Optional: $env:CUDA_PATH='C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8'
# Optional: $env:HIGGS_ENGINE_DIR='C:\YOUR_LEGAL_HIGGS_INSTALL\resources\engine'
# Optional: $env:HIGGS_MODEL_ROOT='C:\YOUR_LEGAL_HIGGS_INSTALL\models\higgs-q4_k_m'
# No platform cookies, reference voices, API credentials or process registrations.

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = 1                      # 分析結果暫存在記憶體，必須單一 worker（不要調高）
threads = int(os.environ.get("THREADS", "4"))
timeout = 120                    # 大檔案分析可能較久
accesslog = "-"

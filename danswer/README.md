config.json 
```
{
  "local_workspace_path": "/Users/jayghiya/Documents/unoplat/danswer/backend",
  "output_path": "/Users/jayghiya/Documents/unoplat",
  "output_file_name": "unoplat_danswer_backend.md",
  "codebase_name": "danswer",
  "programming_language": "python",
  "repo": {
    "download_url": "archguard/archguard",
    "download_directory": "/Users/jayghiya/Documents/unoplat"
  },
  "api_tokens": {
    "github_token": "yourpattoken"
  },
  "llm_provider_config": {
      "openai": {
        "api_key": "youropenaikey",
        "model": "gpt-4o-mini",
        "model_type" : "chat",
        "max_tokens": 3096,
        "temperature": 0.0
      }
  },
  "logging_handlers": [
    {
        "sink": "~/Documents/unoplat/app.log",
        "format": "<green>{time:YYYY-MM-DD at HH:mm:ss}</green> | <level>{level}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        "rotation": "10 MB",
        "retention": "10 days",
        "level": "DEBUG"
    }
  ]
}
```

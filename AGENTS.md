# Project Working Notes

- Before starting work in this repository, read `PROJECT_MEMORY.md`. Update it when the user gives new standing instructions, corrections, lessons learned, or project-specific preferences.
- Treat project text files as UTF-8. On Windows, PowerShell or cmd may render Chinese UTF-8 bytes as GBK mojibake; verify bytes or decode as UTF-8 before changing text that only looks corrupted in the terminal.
- Use `.env` for local runtime/build configuration.
- Use port `8090` for local debugging. Docker Compose exposes nginx as `8090:80`; do not run another service on the same port at the same time.
- After finishing a code task, rebuild/verify both backend and frontend so the local debug target is usable.
- After finishing a code task, commit and push the completed work to the `design/lm` branch on both GitHub and Gitee.

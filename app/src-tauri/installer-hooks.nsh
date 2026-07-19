; Aether Intelligence — NSIS installer hooks.
;
; Aether Intelligence can talk to Claude two ways:
;   1. a Claude Pro/Max subscription — this needs the Claude Code CLI on the machine,
;      because the Agent SDK runs on top of it; or
;   2. a billed API key — needs nothing extra.
;
; Everything else the app needs (Python, all its libraries) is bundled inside
; backend.exe, so the CLI is the ONE external dependency — and only for path 1.
; So we CHECK for it at install time and tell the user how to get it, but we never
; block the install: an API-key user doesn't need it at all.

!macro NSIS_HOOK_POSTINSTALL
  ; `where claude` searches PATH; exit code 0 means it found the CLI.
  nsExec::ExecToStack '"$SYSDIR\cmd.exe" /c where claude'
  Pop $0 ; exit code
  Pop $1 ; output (discarded)

  StrCmp $0 "0" claude_ok claude_missing

  claude_missing:
    MessageBox MB_YESNO|MB_ICONINFORMATION \
"Aether Intelligence is installed.$\n$\n\
One optional extra: the Claude Code CLI was NOT found on this PC.$\n$\n\
It is required ONLY if you want to use your Claude Pro/Max subscription. If you plan \
to use an API key instead, you can ignore this and everything will work.$\n$\n\
Open the Claude Code install instructions now?" \
      /SD IDNO IDNO claude_ok
    ExecShell "open" "https://docs.claude.com/en/docs/claude-code/setup"
    Goto claude_ok

  claude_ok:
!macroend

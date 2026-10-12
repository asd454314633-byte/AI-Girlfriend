# Switch-Girlfriend-Avatar.ps1 -- 墓碑（TOMBSTONE）。本入口已作废，不再转发任何动作。
#
# 作废原因（TASK_ID = AI_GIRLFRIEND_RUNTIME_LIFECYLE_FINAL_REPAIR_01 §三「禁止四份不同的启动逻辑」）：
#   上一版是「薄转发壳」，把 Switch 转给 runtime\windows\Avatar-Supervisor.ps1 -Action Switch。
#   但 Supervisor 的 Switch 路径（Invoke-AvatarSwitchBody，约 :1201-:1244）按其自述
#   （:1203 "pointer stays the switch script's business (one writer)"）：
#     - 不提交选人的事实源指针（runtime\windows\ 下的 active_avatar 指针文件）；
#     - 也没有任何回滚（失败只回 verdict，旧人不会被打回去）。
#   于是从本入口换人，下一次冷启动会静默退回旧的人——这是 MUST FIX 缺陷。
#
# 唯一带「指针提交 + 回滚」语义的切换实现在：
#   runtime\windows\Switch-Girlfriend-Avatar.ps1 -Target <id> [-Mode Switch|Sync|Restart]
#   它是 active_avatar 指针的唯一写者（Write-AvatarPointer / Restore-PreviousAvatar），
#   进程侧效果仍全部委派给 Avatar-Supervisor 的原语，不构成第二份启动逻辑。
#   真实调用方 runtime\ui\app\avatar_admin.py（SWITCH_SCRIPT，:96 与 :927）本来就
#   直接指向那份文件，不经这里。
#
# 2026-09-25 全仓审计结论：本文件零真实调用者——没有任何 .ps1/.cmd/.py/.js 以 -File
#   或 dot-source 调用它，计划任务动作里也没有它；提到它的只有历史注释/文档
#   （server.py:714、avatar_admin.py 模块注释、AVATAR_FRONTEND_MANAGER_01_PRD.md:287），
#   因此按任务方案 B 替换为显式报错墓碑：任何残留调用都会立刻暴露，
#   而不是静默走到不安全的路径。
#
# 改前原文逐字节备份：scripts\Switch-Girlfriend-Avatar.ps1.bak_switchshim

param(
    # 宽收旧参数面（-Target/-Mode 及一切多余实参），保证 ERROR= 行必然先打出来，
    # 不会被 PowerShell 参数绑定报错抢先——那等于把墓碑自己弄哑。
    [string]$Target = '',
    [string]$Mode = '',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

# 尽力在切换审计日志里记一行（沿用旧文件名 avatar-switch.log），让「谁还在敲这个
# 作废入口」可追溯。墓碑不得因与留言无关的原因失败，所以整段吞异常、且只在
# 日志目录已存在时追加，绝不新建目录。
try {
    $logRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'logs\runtime'
    if (Test-Path -LiteralPath $logRoot) {
        $line = '[{0}] AVATAR_SWITCH_TOMBSTONE_CALLED target={1} mode={2} rest={3}' -f `
            (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $Target, $Mode, ($Rest -join ',')
        Add-Content -LiteralPath (Join-Path $logRoot 'avatar-switch.log') -Value $line -Encoding UTF8
    }
} catch {}

# 契约：第一行永远是这条 ERROR=，任何残留调用者（人或脚本）都能 grep 到。
Write-Output 'ERROR=SUPERSEDED_BY_runtime_windows_Switch-Girlfriend-Avatar'
Write-Output 'USE=runtime\windows\Switch-Girlfriend-Avatar.ps1 -Target <id> [-Mode Switch|Sync|Restart]'
[Console]::Error.WriteLine('ERROR=SUPERSEDED_BY_runtime_windows_Switch-Girlfriend-Avatar')
# 非 0 退出：旧壳的退出码来自 Supervisor（0=成功，非 0=失败）；这里用 2 明确表示
# 「入口已作废」，与旧的失败语义区分开。
exit 2

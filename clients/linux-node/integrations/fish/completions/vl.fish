# Fish completions for `vl`. Install to ~/.config/fish/completions/vl.fish.

complete -c vl -f
complete -c vl -n __fish_use_subcommand -a status -d "Show aggregated daemon state"
complete -c vl -n __fish_use_subcommand -a setup -d "Fetch the latest client and install the runtime files"
complete -c vl -n __fish_use_subcommand -a doctor -d "Report diagnostics"
complete -c vl -n __fish_use_subcommand -a open -d "Open the Web UI for this node"
complete -c vl -n __fish_use_subcommand -a web -d "Open the Web UI"
complete -c vl -n __fish_use_subcommand -a tui -d "Open the terminal renderer"
complete -c vl -n __fish_use_subcommand -a chat -d "Open the terminal renderer"
complete -c vl -n __fish_use_subcommand -a renderer -d "Renderer lifecycle"
complete -c vl -n __fish_use_subcommand -a connect -d "Connect a companion"
complete -c vl -n __fish_use_subcommand -a companion -d "List and select companions"
complete -c vl -n __fish_use_subcommand -a disconnect -d "Release the presentation"
complete -c vl -n __fish_use_subcommand -a mode -d "Set interaction mode"
complete -c vl -n __fish_use_subcommand -a break -d "Signal a break boundary"
complete -c vl -n __fish_use_subcommand -a task -d "Task controls"
complete -c vl -n __fish_use_subcommand -a ask -d "Send one message"
complete -c vl -n __fish_use_subcommand -a travel -d "Prepare a handoff"
complete -c vl -n __fish_use_subcommand -a attention -d "Attention overrides"
complete -c vl -n __fish_use_subcommand -a shell -d "Report shell activity"
complete -c vl -n __fish_use_subcommand -a screen -d "Screen vision capture session"
complete -c vl -n __fish_use_subcommand -a voice -d "Continuous voice session controls"
complete -c vl -n __fish_use_subcommand -a daemon -d "Run the host daemon"
complete -c vl -n __fish_use_subcommand -a version -d "Print version"

# Global flag
complete -c vl -l config -r -d "Path to config.toml"

# Subcommand actions
complete -c vl -n "__fish_seen_subcommand_from mode" -a "quiet chat focus away" -f
complete -c vl -n "__fish_seen_subcommand_from companion" -a "list select" -f
complete -c vl -n "__fish_seen_subcommand_from renderer" -a "status attach detach" -f
complete -c vl -n "__fish_seen_subcommand_from renderer attach" -l companion -r -f
complete -c vl -n "__fish_seen_subcommand_from renderer detach" -l renderer-id -r -f
complete -c vl -n "__fish_seen_subcommand_from task" -a "done" -f
complete -c vl -n "__fish_seen_subcommand_from attention" -a "override clear" -f
complete -c vl -n "__fish_seen_subcommand_from attention override" -a "focus away break" -f
complete -c vl -n "__fish_seen_subcommand_from attention override" -l seconds -r -f
complete -c vl -n "__fish_seen_subcommand_from shell" -a "register started finished closed" -f
complete -c vl -n "__fish_seen_subcommand_from screen" -a "start stop status" -f
complete -c vl -n "__fish_seen_subcommand_from screen start" -l companion -r -f
complete -c vl -n "__fish_seen_subcommand_from screen start" -l profile -a "economy balanced detail" -f
complete -c vl -n "__fish_seen_subcommand_from screen start" -l mode -a "continuous one_shot" -f
complete -c vl -n "__fish_seen_subcommand_from voice" -a "doctor devices start stop status commit interrupt" -f
complete -c vl -n "__fish_seen_subcommand_from voice start" -l mode -a "continuous push_to_talk text" -f
complete -c vl -n "__fish_seen_subcommand_from voice doctor" -l json -f
complete -c vl -n "__fish_seen_subcommand_from voice commit" -l stdin -f
complete -c vl -n "__fish_seen_subcommand_from voice commit" -l release -f
complete -c vl -n "__fish_seen_subcommand_from daemon" -a "run" -f
complete -c vl -n "__fish_seen_subcommand_from doctor" -l json -f
complete -c vl -n "__fish_seen_subcommand_from ask" -l stdin -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l server -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l dist-url -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l version -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l wheel -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l github -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l github-token -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l github-api -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l token -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l token-file -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l node-id -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l device-id -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l label -r -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l backend -a "niri none" -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l attention -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l no-systemd -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l clobber -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l dry-run -f
complete -c vl -n "__fish_seen_subcommand_from setup" -l json -f

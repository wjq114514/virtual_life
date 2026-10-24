# Virtual Life fish integration. Opt-in via interaction.fish_activity_enabled.
#
# Only opaque command tokens are sent; command text, arguments, environment and
# output never leave the shell. Add to config.fish:
#   source ~/.config/fish/conf.d/virtual-life.fish

if status is-interactive
    set -gx VIRTUAL_LIFE_SESSION_ID (string join '' (random) (random))
    command vl shell register >/dev/null 2>&1

    function __vl_preexec --on-event fish_preexec
        set -gx VIRTUAL_LIFE_CMD_TOKEN (string join '' (random) (random))
        command vl shell started >/dev/null 2>&1
    end

    function __vl_postexec --on-event fish_postexec
        command vl shell finished >/dev/null 2>&1
    end

    function __vl_exit --on-event fish_exit
        command vl shell closed >/dev/null 2>&1
    end
end

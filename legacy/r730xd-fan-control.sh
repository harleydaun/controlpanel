#!/bin/bash
#
# r730xd-fan-control.sh  (v2 - smooth curve controller)
# Dell PowerEdge R730xd fan control via iDRAC8 IPMI
#
# Strategy: quiet-first proportional control.
# - Uses MAX of CPU1 and CPU2 temps (R730xd has asymmetric thermal layout).
# - Continuous fan curve with linear interpolation (1% resolution) instead
#   of coarse zones, so there are no big audible steps to cycle between.
# - Asymmetric EMA smoothing of temperature: tracks rises quickly (safety),
#   falls slowly (quiet), so transient dips/spikes don't move the fan.
# - Deadband: fan does not move unless it is >= DEADBAND_PCT away from the
#   curve target.
# - Slew limiting: fan may rise fast, but only decays 1%/poll, and only
#   after the target has been lower for DOWN_HOLD_POLLS consecutive polls.
# - Emergency handoff to Dell auto-control uses the RAW temperature
#   (never the smoothed value) so smoothing cannot delay safety response.
#

set -u

# ============================================================
# CONFIGURATION
# ============================================================

IPMI_CMD="ipmitool"
POLL_INTERVAL=10
FAILSAFE_PERCENT=40

# Fan curve points: "tempC:fan_percent", ascending temperature.
# Fan target is linearly interpolated between points.
# Below the first point -> first fan%. Above the last -> last fan%
# (until EMERGENCY_TEMP takes over).
#
# Tuning for a media server: max(CPU1,CPU2) around 74-76C under load is
# fine for E5-26xx v3/v4 Xeons (Dell's own throttle points are ~85-90C+).
# If you want it cooler at the cost of noise, shift the temps down a
# few degrees or raise the fan% values.
declare -a FAN_CURVE=(
    "55:12"
    "65:16"
    "70:20"
    "74:26"
    "77:34"
    "79:45"
)

# Raw temp >= this -> emergency: hand control to Dell automatic
EMERGENCY_TEMP=80
# Reclaim manual control when raw temp falls to this or below
EMERGENCY_CLEAR_TEMP=76

# --- Smoothing / anti-cycling ---------------------------------------
# EMA alpha (numerator over ALPHA_DEN). Higher = reacts faster.
ALPHA_UP_NUM=50        # temp rising: track quickly (~2 polls to mostly catch up)
ALPHA_DOWN_NUM=15      # temp falling: track slowly (~1 min time constant)
ALPHA_DEN=100

DEADBAND_PCT=2         # don't touch the fan for target errors smaller than this
MAX_STEP_UP=8          # max fan % increase per poll
MAX_STEP_DOWN=1        # max fan % decrease per poll (slow silent decay)
DOWN_HOLD_POLLS=6      # target must stay below current fan for this many
                       # consecutive polls (6 x 10s = 60s) before decreasing

LOG_FILE="/var/log/r730xd-fan-control.log"
LOG_MAX_LINES=5000

# ============================================================
# STATE
# ============================================================

CURRENT_FAN_PCT=-1
SMOOTH_X10=-1          # EMA of max CPU temp, in tenths of a degree; -1 = unset
DOWN_COUNT=0           # consecutive polls with target below current fan
EMERGENCY_MODE=0
LAST_HEARTBEAT_TS=0
HEARTBEAT_INTERVAL=300 # log a steady-state line every 5 min minimum

# ============================================================
# HELPERS
# ============================================================

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true

    if [[ -f "$LOG_FILE" ]]; then
        local lines
        lines=$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)
        if (( lines > LOG_MAX_LINES )); then
            tail -n $((LOG_MAX_LINES / 2)) "$LOG_FILE" > "${LOG_FILE}.tmp" \
                && mv "${LOG_FILE}.tmp" "$LOG_FILE"
        fi
    fi
}

ipmi_local() {
    $IPMI_CMD "$@" 2>&1
}

get_max_cpu_temp() {
    # Reads both CPU sensors, returns highest.
    local output
    output=$(ipmi_local sdr type temperature 2>/dev/null) || return 1

    local max=0
    local temp
    while IFS= read -r line; do
        if [[ "$line" =~ Temp[[:space:]]*\| ]] && [[ "$line" =~ ([0-9]+)[[:space:]]*degrees[[:space:]]*C ]]; then
            temp="${BASH_REMATCH[1]}"
            if (( temp > max )); then
                max=$temp
            fi
        fi
    done <<< "$output"

    if (( max == 0 )); then
        return 1
    fi
    echo "$max"
}

enable_manual_control() {
    ipmi_local raw 0x30 0x30 0x01 0x00 >/dev/null
    return $?
}

enable_auto_control() {
    ipmi_local raw 0x30 0x30 0x01 0x01 >/dev/null
    return $?
}

set_fan_percent() {
    local pct=$1
    local hex
    hex=$(printf '0x%02x' "$pct")

    enable_manual_control || return 1
    ipmi_local raw 0x30 0x30 0x02 0xff "$hex" >/dev/null
    return $?
}

# --------------------------------------------------------------------
# Curve interpolation.
# Input: smoothed temp in tenths of a degree. Output: target fan %.
# --------------------------------------------------------------------
curve_target() {
    local tx10=$1
    local n=${#FAN_CURVE[@]}

    local first="${FAN_CURVE[0]}"
    local last="${FAN_CURVE[$((n - 1))]}"
    local t_lo=$(( ${first%%:*} * 10 ))
    local f_lo=${first##*:}
    local t_hi=$(( ${last%%:*} * 10 ))
    local f_hi=${last##*:}

    if (( tx10 <= t_lo )); then echo "$f_lo"; return; fi
    if (( tx10 >= t_hi )); then echo "$f_hi"; return; fi

    local i p t1 f1 q t0 f0
    for (( i=1; i<n; i++ )); do
        p="${FAN_CURVE[$i]}"
        t1=$(( ${p%%:*} * 10 ))
        f1=${p##*:}
        if (( tx10 <= t1 )); then
            q="${FAN_CURVE[$((i - 1))]}"
            t0=$(( ${q%%:*} * 10 ))
            f0=${q##*:}
            # Linear interpolation, rounded to nearest whole percent.
            echo $(( f0 + ( (tx10 - t0) * (f1 - f0) + (t1 - t0) / 2 ) / (t1 - t0) ))
            return
        fi
    done
    echo "$f_hi"
}

# --------------------------------------------------------------------
# Asymmetric EMA update. $1 = raw temp in whole degrees C.
# Rises track fast (ALPHA_UP), falls track slow (ALPHA_DOWN).
# --------------------------------------------------------------------
update_smooth() {
    local raw_x10=$(( $1 * 10 ))

    if (( SMOOTH_X10 < 0 )); then
        SMOOTH_X10=$raw_x10
        return
    fi

    local delta=$(( raw_x10 - SMOOTH_X10 ))
    local num=$ALPHA_DOWN_NUM
    if (( delta > 0 )); then
        num=$ALPHA_UP_NUM
    fi

    local step=$(( delta * num / ALPHA_DEN ))
    # Integer division can stall convergence on tiny deltas; nudge by 0.1C.
    if (( step == 0 && delta != 0 )); then
        if (( delta > 0 )); then step=1; else step=-1; fi
    fi

    SMOOTH_X10=$(( SMOOTH_X10 + step ))
}

# ============================================================
# CLEANUP / SIGNALS
# ============================================================

cleanup() {
    log "Shutting down. Failsafe ${FAILSAFE_PERCENT}% then handing back to Dell auto."
    set_fan_percent "$FAILSAFE_PERCENT" || log "WARN: failsafe set failed during cleanup"
    sleep 1
    enable_auto_control || log "WARN: could not restore Dell auto control"
    log "Stopped."
    exit 0
}

emergency_handoff() {
    log "EMERGENCY: raw temp >= ${EMERGENCY_TEMP}C. Failsafe ${FAILSAFE_PERCENT}% + Dell auto control."
    set_fan_percent "$FAILSAFE_PERCENT" || true
    sleep 1
    enable_auto_control || log "WARN: emergency auto-control switch failed"
    EMERGENCY_MODE=1
    CURRENT_FAN_PCT=-1
    SMOOTH_X10=-1
    DOWN_COUNT=0
}

trap cleanup SIGTERM SIGINT SIGHUP

# ============================================================
# MAIN
# ============================================================

main() {
    log "Starting R730xd fan controller v2 (PID $$)"
    log "Failsafe=${FAILSAFE_PERCENT}%, emergency=${EMERGENCY_TEMP}C (clear<=${EMERGENCY_CLEAR_TEMP}C), poll=${POLL_INTERVAL}s"
    log "Curve: ${FAN_CURVE[*]}"
    log "Smoothing: alpha up=${ALPHA_UP_NUM}/${ALPHA_DEN} down=${ALPHA_DOWN_NUM}/${ALPHA_DEN}, deadband=${DEADBAND_PCT}%, step up<=${MAX_STEP_UP}%/poll, down=${MAX_STEP_DOWN}%/poll after ${DOWN_HOLD_POLLS} polls"

    if ! command -v "$IPMI_CMD" >/dev/null 2>&1; then
        log "FATAL: ipmitool not found."
        exit 1
    fi
    if ! ipmi_local mc info >/dev/null 2>&1; then
        log "FATAL: cannot talk to iDRAC. Is ipmi_si loaded?"
        exit 1
    fi

    # Known-good baseline at startup
    log "Initial failsafe ${FAILSAFE_PERCENT}%"
    if ! set_fan_percent "$FAILSAFE_PERCENT"; then
        log "FATAL: cannot set initial fan speed"
        exit 1
    fi
    CURRENT_FAN_PCT=$FAILSAFE_PERCENT

    # Jump straight to the curve target for the current temp
    local temp
    if temp=$(get_max_cpu_temp); then
        update_smooth "$temp"
        local pct
        pct=$(curve_target "$SMOOTH_X10")
        if set_fan_percent "$pct"; then
            CURRENT_FAN_PCT=$pct
            log "Initial temp=${temp}C -> fan ${pct}%"
            LAST_HEARTBEAT_TS=$(date +%s)
        fi
    fi

    # Main loop
    while true; do
        if ! temp=$(get_max_cpu_temp); then
            log "WARN: temp read failed. Failsafe ${FAILSAFE_PERCENT}%."
            set_fan_percent "$FAILSAFE_PERCENT" || log "WARN: failsafe set failed"
            CURRENT_FAN_PCT=$FAILSAFE_PERCENT
            SMOOTH_X10=-1
            DOWN_COUNT=0
            sleep "$POLL_INTERVAL"
            continue
        fi

        # ---- Emergency handling: ALWAYS on raw temp, never smoothed ----
        if (( EMERGENCY_MODE == 1 )); then
            if (( temp <= EMERGENCY_CLEAR_TEMP )); then
                log "Raw temp=${temp}C cleared emergency. Reclaiming manual control."
                EMERGENCY_MODE=0
                update_smooth "$temp"
                local pct
                pct=$(curve_target "$SMOOTH_X10")
                if set_fan_percent "$pct"; then
                    CURRENT_FAN_PCT=$pct
                    log "Temp=${temp}C -> fan ${pct}%"
                    LAST_HEARTBEAT_TS=$(date +%s)
                fi
            fi
            sleep "$POLL_INTERVAL"
            continue
        fi

        if (( temp >= EMERGENCY_TEMP )); then
            log "Raw temp=${temp}C triggered emergency threshold"
            emergency_handoff
            sleep "$POLL_INTERVAL"
            continue
        fi

        # ---- Smooth control ----
        update_smooth "$temp"
        local target diff new_pct
        target=$(curve_target "$SMOOTH_X10")
        diff=$(( target - CURRENT_FAN_PCT ))
        new_pct=$CURRENT_FAN_PCT

        if (( diff >= DEADBAND_PCT )); then
            # Rise promptly, but slew-limited
            local step=$diff
            (( step > MAX_STEP_UP )) && step=$MAX_STEP_UP
            new_pct=$(( CURRENT_FAN_PCT + step ))
            DOWN_COUNT=0
        elif (( diff <= -DEADBAND_PCT )); then
            # Only decay after the target has been lower for a sustained period
            DOWN_COUNT=$(( DOWN_COUNT + 1 ))
            if (( DOWN_COUNT >= DOWN_HOLD_POLLS )); then
                new_pct=$(( CURRENT_FAN_PCT - MAX_STEP_DOWN ))
            fi
        else
            DOWN_COUNT=0
        fi

        if (( new_pct != CURRENT_FAN_PCT )); then
            if set_fan_percent "$new_pct"; then
                local direction="UP"
                (( new_pct < CURRENT_FAN_PCT )) && direction="DOWN"
                log "Temp=${temp}C smooth=$((SMOOTH_X10 / 10)).$((SMOOTH_X10 % 10))C target=${target}% ${direction}: ${CURRENT_FAN_PCT}% -> ${new_pct}%"
                CURRENT_FAN_PCT=$new_pct
                LAST_HEARTBEAT_TS=$(date +%s)
            else
                log "WARN: failed to set fan to ${new_pct}%"
            fi
        else
            # Steady state: log heartbeat occasionally
            local now
            now=$(date +%s)
            if (( now - LAST_HEARTBEAT_TS >= HEARTBEAT_INTERVAL )); then
                log "Heartbeat: temp=${temp}C smooth=$((SMOOTH_X10 / 10)).$((SMOOTH_X10 % 10))C target=${target}% fan=${CURRENT_FAN_PCT}%"
                LAST_HEARTBEAT_TS=$now
            fi
        fi

        sleep "$POLL_INTERVAL"
    done
}

main "$@"

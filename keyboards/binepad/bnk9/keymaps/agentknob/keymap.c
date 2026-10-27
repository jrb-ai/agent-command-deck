// Copyright 2026 Jon Regan-Beasley
// SPDX-License-Identifier: GPL-2.0-or-later

// BNK9 "agent knob" keymap.
//
// The pad is a pure control surface: no key emits a normal keystroke.
// Key/knob events are reported to the host daemon (bnk9d) over raw HID,
// and the daemon drives every LED. No key emits a keystroke of its own;
// the one function that stays in firmware, so it works even when the
// daemon is down, is knob rotation as a native mouse wheel.
//
// Raw HID protocol (32-byte reports)
//   device -> host:
//     [0x01, key_id, pressed]   key event; key_id 0=knob push, 1..9 keys
//     [0x02, dir]               knob rotation in EFFORT mode (1=cw, 0=ccw)
//     [0x03, mode]              knob mode changed (0=scroll, 1=effort)
//     [0x7F, PROTO_VERSION]     pong
//   host -> device:
//     [0x10, idx(0-8), r, g, b, mode]  set key LED; mode 0=off 1=solid
//                                      2=flash slow 3=flash fast
//                                      4=breathe
//     [0x20, value]             global brightness 0-255
//     [0x21]                    all LEDs off
//     [0x22, anim]              overlay on the six agent keys; 0=normal
//                               per-key rendering, 1=ultracode pulse,
//                               2=flowing rainbow
//     [0x23, scroll, effort]    knob pulses required per action, 1-16;
//                               raise to make a loose knob less twitchy
//     [0x7F]                    ping (any received report marks host alive)

#include QMK_KEYBOARD_H
#include "raw_hid.h"

#define PROTO_VERSION 1
#define REPORT_SIZE 32  // raw HID endpoint size
#define HOST_TIMEOUT_MS 10000
#define EFFORT_MODE_TIMEOUT_MS 3000
enum overlay_anim { ANIM_NONE = 0, ANIM_ULTRACODE = 1, ANIM_RAINBOW = 2 };

enum custom_keycodes {
    BK_KNB = SAFE_RANGE,
    BK_A1, BK_A2, BK_A3, BK_A4, BK_A5, BK_A6,
    BK_ACC, BK_CAN, BK_SPK,
};

enum led_mode { LED_OFF = 0, LED_SOLID, LED_FLASH_SLOW, LED_FLASH_FAST,
                LED_BREATHE };
enum knob_mode { KNOB_SCROLL = 0, KNOB_EFFORT = 1 };

static uint8_t  led_rgb[9][3];
static uint8_t  led_mode[9];
static uint8_t  brightness   = 255;
static uint32_t last_host_ms = 0;
static bool     host_seen    = false;
static uint8_t  knob_mode    = KNOB_SCROLL;
static uint32_t effort_deadline = 0;
static uint8_t  overlay      = ANIM_NONE;
// Pulses required per action. The encoder reports more than one pulse
// per physical click on some units, which makes a freely spinning knob
// scroll far more than intended.
static uint8_t  scroll_div   = 1;
static uint8_t  effort_div   = 1;
static int8_t   scroll_accum = 0;
static int8_t   effort_accum = 0;

const uint16_t PROGMEM keymaps[][MATRIX_ROWS][MATRIX_COLS] = {
    [0] = LAYOUT(
        BK_KNB,
        BK_A1,  BK_A2,  BK_A3,
        BK_A4,  BK_A5,  BK_A6,
        BK_ACC, BK_CAN, BK_SPK
    ),
};

static bool host_alive(void) {
    return host_seen && timer_elapsed32(last_host_ms) < HOST_TIMEOUT_MS;
}

static void send_event(uint8_t kind, uint8_t a, uint8_t b) {
    uint8_t report[REPORT_SIZE] = {0};
    report[0] = kind;
    report[1] = a;
    report[2] = b;
    raw_hid_send(report, REPORT_SIZE);
}

static void set_knob_mode(uint8_t mode) {
    if (knob_mode == mode) return;
    knob_mode = mode;
    if (mode == KNOB_EFFORT) effort_deadline = timer_read32() + EFFORT_MODE_TIMEOUT_MS;
    send_event(0x03, knob_mode, 0);
}

void raw_hid_receive(uint8_t *data, uint8_t length) {
    last_host_ms = timer_read32();
    host_seen    = true;
    switch (data[0]) {
        case 0x10: {
            uint8_t idx = data[1];
            if (idx < 9) {
                led_rgb[idx][0] = data[2];
                led_rgb[idx][1] = data[3];
                led_rgb[idx][2] = data[4];
                led_mode[idx]   = data[5] <= LED_BREATHE ? data[5] : LED_SOLID;
            }
            break;
        }
        case 0x20:
            brightness = data[1];
            break;
        case 0x21:
            for (uint8_t i = 0; i < 9; i++) led_mode[i] = LED_OFF;
            overlay = ANIM_NONE;
            break;
        case 0x23:
            if (data[1] >= 1 && data[1] <= 16) scroll_div = data[1];
            if (data[2] >= 1 && data[2] <= 16) effort_div = data[2];
            scroll_accum = 0;
            effort_accum = 0;
            break;
        case 0x22:
            if (data[1] <= ANIM_RAINBOW) {
                overlay = data[1];
            }
            break;
        case 0x7F: {
            uint8_t report[REPORT_SIZE] = {0};
            report[0] = 0x7F;
            report[1] = PROTO_VERSION;
            raw_hid_send(report, REPORT_SIZE);
            break;
        }
    }
}

static uint8_t keycode_to_id(uint16_t keycode) {
    switch (keycode) {
        case BK_KNB: return 0;
        case BK_A1:  return 1;
        case BK_A2:  return 2;
        case BK_A3:  return 3;
        case BK_A4:  return 4;
        case BK_A5:  return 5;
        case BK_A6:  return 6;
        case BK_ACC: return 7;
        case BK_CAN: return 8;
        case BK_SPK: return 9;
        default:     return 0xFF;
    }
}

bool process_record_user(uint16_t keycode, keyrecord_t *record) {
    uint8_t id = keycode_to_id(keycode);
    if (id == 0xFF) return true;

    if (keycode == BK_KNB && record->event.pressed && host_alive()) {
        set_knob_mode(knob_mode == KNOB_SCROLL ? KNOB_EFFORT : KNOB_SCROLL);
    }

    send_event(0x01, id, record->event.pressed ? 1 : 0);
    return false;
}

// Count pulses until enough have accumulated in one direction. Turning
// back the other way resets, so a reversal responds immediately rather
// than having to work off the pulses already banked.
static bool step_ready(int8_t *accum, uint8_t divisor, bool clockwise) {
    int8_t delta = clockwise ? 1 : -1;
    if ((*accum > 0 && delta < 0) || (*accum < 0 && delta > 0)) *accum = 0;
    *accum += delta;
    if (*accum >= (int8_t)divisor || *accum <= -(int8_t)divisor) {
        *accum = 0;
        return true;
    }
    return false;
}

bool encoder_update_user(uint8_t index, bool clockwise) {
    if (knob_mode == KNOB_EFFORT && host_alive()) {
        effort_deadline = timer_read32() + EFFORT_MODE_TIMEOUT_MS;
        if (step_ready(&effort_accum, effort_div, clockwise)) {
            send_event(0x02, clockwise ? 1 : 0, 0);
        }
    } else if (step_ready(&scroll_accum, scroll_div, clockwise)) {
        // Right/clockwise scrolls the page down, matching macOS natural
        // scrolling: content follows the direction you turn.
        tap_code(clockwise ? MS_WHLU : MS_WHLD);
    }
    return false;
}

void housekeeping_task_user(void) {
    if (knob_mode == KNOB_EFFORT && timer_expired32(timer_read32(), effort_deadline)) {
        set_knob_mode(KNOB_SCROLL);
    }
}

void keyboard_post_init_user(void) {
    rgb_matrix_enable_noeeprom();
    rgb_matrix_mode_noeeprom(RGB_MATRIX_SOLID_COLOR);
    rgb_matrix_sethsv_noeeprom(0, 0, 0);  // black base; indicators paint over
}

static void render_key(uint8_t led, uint8_t i) {
    uint8_t r = led_rgb[i][0], g = led_rgb[i][1], b = led_rgb[i][2];
    uint8_t scale;
    switch (led_mode[i]) {
        case LED_SOLID:      scale = 255; break;
        case LED_FLASH_SLOW: scale = (timer_read() % 800) < 400 ? 255 : 0; break;
        case LED_FLASH_FAST: scale = (timer_read() % 250) < 125 ? 255 : 0; break;
        case LED_BREATHE: {
            // Smooth fade rather than on/off: "finished, no rush", as
            // against the flashing that means something is blocked.
            uint16_t t   = timer_read() % 3600;
            uint16_t tri = t < 1800 ? t : 3600 - t;             // 0..1800
            scale = 20 + (uint8_t)((uint32_t)tri * 235 / 1800);  // 20..255
            break;
        }
        default:             scale = 0; break;
    }
    uint16_t v = (uint16_t)scale * brightness / 255;
    rgb_matrix_set_color(led,
                         (uint16_t)r * v / 255,
                         (uint16_t)g * v / 255,
                         (uint16_t)b * v / 255);
}

bool rgb_matrix_indicators_advanced_user(uint8_t led_min, uint8_t led_max) {
    if (!host_alive()) {
        // Daemon absent: dim slow breathing so the pad reads as "asleep".
        uint16_t t = timer_read() % 4000;
        uint8_t  v = t < 2000 ? t / 50 : (4000 - t) / 50;  // 0..40
        for (uint8_t led = led_min; led < led_max; led++) {
            rgb_matrix_set_color(led, v / 2, v / 2, v);
        }
        return false;
    }
    // Ultracode pulse, hard and fast: a short period with a squared
    // envelope, so it snaps to full rather than easing like the calm
    // "your turn" breathe. Confined to the six agent keys; the bottom
    // row keeps its own colours so accept/cancel stay readable.
    uint8_t pulse_r = 0, pulse_b = 0;
    if (overlay == ANIM_ULTRACODE) {
        uint16_t t   = timer_read() % 900;
        uint16_t tri = t < 450 ? t : 900 - t;                    // 0..450
        uint32_t sq  = (uint32_t)tri * tri;                      // 0..202500
        uint8_t  env = 30 + (uint8_t)(sq * 225 / 202500);        // 30..255
        pulse_r = (uint16_t)200 * env / 255 * brightness / 255;
        pulse_b = (uint16_t)255 * env / 255 * brightness / 255;
    }
    for (uint8_t row = 1; row < MATRIX_ROWS; row++) {
        for (uint8_t col = 0; col < MATRIX_COLS; col++) {
            uint8_t led = g_led_config.matrix_co[row][col];
            if (led == NO_LED || led < led_min || led >= led_max) continue;
            uint8_t i = (row - 1) * 3 + col;
            if (overlay == ANIM_ULTRACODE && i < 6) {
                rgb_matrix_set_color(led, pulse_r, 0, pulse_b);
            } else if (overlay == ANIM_RAINBOW && i < 6) {
                // Hue sweeps with time and is offset per key, so the
                // spectrum flows across the six rather than pulsing
                // them in unison.
                HSV hsv = {.h = (uint8_t)((timer_read() >> 3) + i * 42),
                           .s = 255, .v = brightness};
                RGB rgb = hsv_to_rgb(hsv);
                rgb_matrix_set_color(led, rgb.r, rgb.g, rgb.b);
            } else {
                render_key(led, i);
            }
        }
    }
    return false;
}

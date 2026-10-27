// Copyright 2026 Jon Regan-Beasley
// SPDX-License-Identifier: GPL-2.0-or-later

#pragma once

// The daemon owns all lighting; never let the matrix idle-sleep dim it
// out from under a flashing "needs approval" state.
#undef RGB_MATRIX_TIMEOUT
#define RGB_MATRIX_TIMEOUT 0

"""LPPLS bubble indicator (Log-Periodic Power Law Singularity, Sornette et al.).

Model of log price before a critical time tc:

    ln p(t) = A + B (tc - t)^m + C1 (tc - t)^m cos(w ln(tc - t)) + C2 (tc - t)^m sin(w ln(tc - t))

For fixed (tc, m, w) the model is linear in (A, B, C1, C2), so each window is
fitted by a vectorised grid over (tc, m, w) with an ordinary least-squares solve
per grid point (Filimonov & Sornette's linearisation; numpy only).

A fit "qualifies" (the usual DS-LPPLS filters) when
    0.1 <= m <= 0.9, 6 <= w <= 13, tc within 20% of the window after its end,
    damping  D = m|B| / (w|C|) >= 0.5,
    oscillations  O = (w / pi) ln((tc - t1) / (tc - t2)) >= 2.5,
    the fit explains the window well (R^2 >= min_r2),
    it beats a plain exponential trend (SSE at most half of a log-linear fit's),
    and its oscillations stand out from the noise (amplitude >= 1.5 x residual sd).
The last two reject smooth exponential rallies, which the classic filters accept.
B < 0 is a positive bubble (accelerating rise -> crash risk); B > 0 a negative
bubble (accelerating fall -> rebound potential).

The confidence on a day is the share of windows (e.g. 30..120 days ending that
day) whose fit qualifies. It is a hazard indicator, not a crash-date forecast:
LPPLS has many false alarms and is used here only to stop adding risk.
"""
from __future__ import annotations

import numpy as np

DAY_MS = 86_400_000
M_GRID = np.linspace(0.1, 0.9, 9)
W_GRID = np.linspace(6.0, 13.0, 8)


def fit_window(t: np.ndarray, y: np.ndarray, tc_steps: int = 8) -> dict:
    """Best grid fit for one window. t in days (increasing), y = log price."""
    t1, t2 = t[0], t[-1]
    span = t2 - t1
    tcs = t2 + np.linspace(0.02, 0.2, tc_steps) * span            # (K,)
    dt = tcs[:, None] - t[None, :]                                # (K, N) all > 0
    ldt = np.log(dt)
    f = dt[:, None, :] ** M_GRID[None, :, None]                   # (K, J, N)
    ph = W_GRID[None, :, None] * ldt[:, None, :]                  # (K, I, N)
    g = f[:, :, None, :] * np.cos(ph)[:, None, :, :]              # (K, J, I, N)
    h = f[:, :, None, :] * np.sin(ph)[:, None, :, :]
    K, J, I, N = g.shape
    ones = np.ones((K, J, I, N))
    X = np.stack([ones, np.broadcast_to(f[:, :, None, :], g.shape), g, h], axis=-1)  # (K,J,I,N,4)
    XtX = np.einsum("kjinp,kjinq->kjipq", X, X)
    Xty = np.einsum("kjinp,n->kjip", X, y)
    ridge = 1e-10 * np.trace(XtX, axis1=-2, axis2=-1)[..., None, None] * np.eye(4)
    beta = np.linalg.solve(XtX + ridge, Xty[..., None])[..., 0]   # (K,J,I,4)
    resid = y[None, None, None, :] - np.einsum("kjinp,kjip->kjin", X, beta)
    sse = (resid ** 2).sum(-1)
    k, j, i = np.unravel_index(np.argmin(sse), sse.shape)
    A, B, C1, C2 = beta[k, j, i]
    best = float(sse[k, j, i])
    sst = ((y - y.mean()) ** 2).sum()
    # Plain exponential growth (a straight line in log price) is NOT a bubble: LPPLS must
    # fit clearly better than it, and its oscillations must stand out from the noise.
    lin = np.polyval(np.polyfit(t, y, 1), t)
    sse_lin = float(((y - lin) ** 2).sum())
    resid_sd = float(np.sqrt(best / max(N - 4, 1)))
    amp = float(np.hypot(C1, C2) * np.mean(dt[k] ** M_GRID[j]))
    return {"tc": float(tcs[k]), "m": float(M_GRID[j]), "w": float(W_GRID[i]), "A": float(A), "B": float(B),
            "C": float(np.hypot(C1, C2)), "r2": float(1 - best / sst) if sst > 0 else 0.0,
            "sse_ratio": best / sse_lin if sse_lin > 0 else 1.0,
            "osc_to_noise": amp / resid_sd if resid_sd > 0 else float("inf"),
            "t1": float(t1), "t2": float(t2)}


def qualifies(fit: dict, min_r2: float = 0.8, max_sse_ratio: float = 0.5, min_osc_to_noise: float = 1.5) -> bool:
    m, w, B, C = fit["m"], fit["w"], fit["B"], fit["C"]
    if not (0.1 <= m <= 0.9 and 6 <= w <= 13) or fit["r2"] < min_r2 or B == 0:
        return False
    if fit["sse_ratio"] > max_sse_ratio or fit["osc_to_noise"] < min_osc_to_noise:
        return False  # no better than a plain trend, or "oscillations" that are just noise
    span = fit["t2"] - fit["t1"]
    if not fit["t2"] < fit["tc"] <= fit["t2"] + 0.2 * span + 1e-9:
        return False
    damping = m * abs(B) / (w * C) if C > 0 else float("inf")
    osc = (w / np.pi) * np.log((fit["tc"] - fit["t1"]) / (fit["tc"] - fit["t2"]))
    return damping >= 0.5 and osc >= 2.5


def confidence(days: np.ndarray, logp: np.ndarray, end: int, windows: list[int], min_r2: float = 0.8) -> dict:
    """Bubble / anti-bubble confidence for the series ending at index `end` (inclusive)."""
    bubble = anti = n = 0
    tcs = []
    for L in windows:
        start = end - L + 1
        if start < 0:
            continue
        fit = fit_window(days[start:end + 1], logp[start:end + 1])
        n += 1
        if qualifies(fit, min_r2):
            if fit["B"] < 0:
                bubble += 1
                tcs.append(fit["tc"] - days[end])
            else:
                anti += 1
    return {"bubble": bubble / n if n else 0.0, "anti": anti / n if n else 0.0, "windows": n,
            "tc_days_median": float(np.median(tcs)) if tcs else None}


def update_series(cached: dict[int, list], daily: list[tuple[int, float]], windows: list[int],
                  min_r2: float = 0.8, need_from: int | None = None) -> dict[int, list]:
    """Incrementally extend a cached {day_ms: [bubble, anti, tc_days]} series.

    Only days on/after `need_from` that are not cached yet are fitted (a day's value never
    changes once its candle has closed), so after the first run each scan fits at most one
    new day. Cached days older than `need_from` are dropped.
    """
    out = {d: v for d, v in cached.items() if need_from is None or d >= need_from}
    if not daily:
        return out
    days = np.array([d / DAY_MS for d, _ in daily], dtype=float)
    logp = np.log(np.array([p for _, p in daily], dtype=float))
    for end in range(min(windows) - 1, len(daily)):
        d = daily[end][0]
        if d in out or (need_from is not None and d < need_from):
            continue
        c = confidence(days, logp, end, windows, min_r2)
        out[d] = [round(c["bubble"], 3), round(c["anti"], 3),
                  None if c["tc_days_median"] is None else round(c["tc_days_median"], 1)]
    return out

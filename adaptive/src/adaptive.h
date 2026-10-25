/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * Adaptive queue count: picks at runtime how many RX queues the NIC spreads
 * traffic over, among the powers of two in [min, max].
 *
 * Mechanism. All max queues are set up at startup. The Open-NIC RSS block
 * steers a packet to queue (hash & mask), and the patched QDMA PMD keeps mask
 * in a single register (see qdma_dev_reta_update). Rewriting that register
 * changes the number of active queues without stopping the port; the polling
 * loop then sweeps only [0, active) plus, after a shrink, the queues being
 * drained.
 *
 * Controller. Every epoch it looks at what the polls of the active queues saw
 * (drain polls and Penalty skips excluded):
 *  - backlog share: after every full poll (32 packets) the packets still
 *    waiting in that queue are read in O(1) from the completion indexes of the
 *    QDMA PMD; the share is the fraction of full polls that left more than half
 *    the ring waiting. On the FPGA it tracks loss: below 1% no loss, above ~4%
 *    loss (the largest backlog does not: it reaches the full ring even without
 *    loss). Above backlog-high, double the queues, which spreads the load over
 *    more rings and, with Aggressive, raises the capacity;
 *  - below backlog-low, halve them, to find the fewest that still keep up;
 *  - empty polls of the sweeps that found work (an idle core spinning on empty
 *    queues wastes nothing): above empty-high, halve the queues.
 * A grow after which the share is still above backlog-high and neither the
 * share nor the cycles per packet (by threshold) went down did not help
 * (Baseline: more queues mean more packets waiting, 32 per visit): it is
 * undone and growing stops for hold epochs, doubled on every further veto.
 * While growing is stopped and the share stays above backlog-high, the
 * controller halves the queues instead: more did not help, fewer might
 * (Baseline drains a backlog faster with fewer queues). For flap-window epochs after a grow, shrink signals are
 * ignored (the drained queues and the new ones settle). A shrink that has to
 * be undone by a grow within flap-window epochs is a flap: shrinking stops for
 * hold epochs, doubled on every further flap (up to 8x), so the controller does
 * not oscillate between two counts. Full polls alone are not a signal: Aggressive
 * drains a backlog with full polls without losing anything. The epoch after
 * every change is discarded, to let the drained queues empty and the caches
 * warm up.
 *
 * With --adaptive-search, two of these rules change, for gains that need
 * several doublings (Aggressive with many flows: 1 -> 2 queues gains a few
 * percent or nothing, 1 -> 128 tens of percent):
 *  - a grow that did not help but did not raise the cycles per packet by more
 *    than threshold either (flat) is not undone at once: the controller jumps
 *    from the same base twice as far (1, 2, 4, 8 octaves) and judges again
 *    against the base, and undoes the grow back to the base only when a jump
 *    is worse or the next one would pass max;
 *  - trying fewer queues is not stopped by the hold of a flap, but it is
 *    judged as well: a shrink that raises the cycles per packet by more than
 *    threshold while the share stays above backlog-high is undone, and
 *    trying fewer stops for hold epochs (doubled as the vetoes);
 *  - after ADAPTIVE_PROBE_EPOCHS without moves with the share between
 *    backlog-low and backlog-high (no signal), halve the queues: a load that
 *    fell leaves the controller on many queues, whose deep rings add tens of
 *    ms of latency (Aggressive on 1024 queues at 3.5 Mpps and 1000 flows: p99
 *    6 ms, against 46 us on 1 queue). A grow within the flap window makes it a
 *    flap, so a probe that fails holds the next one off (hold, doubled).
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ADAPTIVE_MAX_LOG 11 /* 2048 queues */

#define ADAPTIVE_DEFAULT_EPOCH_MS 100
#define ADAPTIVE_DEFAULT_THRESHOLD_PCT 3
#define ADAPTIVE_DEFAULT_HOLD 20
/* Epochs with fewer packets are not used for decisions */
#define ADAPTIVE_MIN_EPOCH_PKTS 1000

#define ADAPTIVE_DEFAULT_EMPTY_HIGH_PCT 50
#define ADAPTIVE_DEFAULT_BACKLOG_HIGH_PCT 5.0
#define ADAPTIVE_DEFAULT_BACKLOG_LOW_PCT 0.5
#define ADAPTIVE_FLAP_WINDOW 10    /* epochs */
#define ADAPTIVE_FLAP_RESET 600    /* epochs without moves that forget the flaps */
#define ADAPTIVE_FLAP_MAX_SHIFT 3  /* hold grows up to 8x */
#define ADAPTIVE_PROBE_EPOCHS 20   /* --adaptive-search: epochs without signal before halving */

struct adaptive_opts {
	bool     enabled;
	uint32_t snapshot_ms;      /* queue snapshots every this many ms, 0 = none */
	uint32_t empty_high_pct;   /* empty polls above which to halve */
	double   backlog_high_pct; /* % of full polls leaving half the ring, to double */
	double   backlog_low_pct;  /* ... below which to halve */
	uint32_t start;         /* initial active queues, 0 = max */
	uint32_t min;           /* fewest active queues */
	uint32_t epoch_ms;      /* measurement epoch */
	uint32_t threshold_pct; /* cost drop that makes a grow worth keeping */
	uint32_t hold;          /* epochs without growing or shrinking after a veto or a flap */
	bool     search;        /* multi-octave grows and judged shrinks (see above) */
};

#define ADAPTIVE_OPT "adaptive"
#define ADAPTIVE_OPT_START "adaptive-start"
#define ADAPTIVE_OPT_MIN "adaptive-min"
#define ADAPTIVE_OPT_EPOCH "adaptive-epoch"
#define ADAPTIVE_OPT_THRESHOLD "adaptive-threshold"
#define ADAPTIVE_OPT_HOLD "adaptive-hold"
#define ADAPTIVE_OPT_EMPTY_HIGH "adaptive-empty-high"
#define ADAPTIVE_OPT_BACKLOG_HIGH "adaptive-backlog-high"
#define ADAPTIVE_OPT_BACKLOG_LOW "adaptive-backlog-low"
#define ADAPTIVE_OPT_SNAPSHOT "snapshot-ms"
#define ADAPTIVE_OPT_SEARCH "adaptive-search"

#define ADAPTIVE_LONG_OPTIONS                                                                      \
	{ADAPTIVE_OPT, no_argument, 0, 0}, {ADAPTIVE_OPT_START, required_argument, 0, 0},              \
	    {ADAPTIVE_OPT_MIN, required_argument, 0, 0},                                               \
	    {ADAPTIVE_OPT_EPOCH, required_argument, 0, 0},                                             \
	    {ADAPTIVE_OPT_THRESHOLD, required_argument, 0, 0},                                         \
	    {ADAPTIVE_OPT_HOLD, required_argument, 0, 0},                                              \
	    {ADAPTIVE_OPT_EMPTY_HIGH, required_argument, 0, 0},                                        \
	    {ADAPTIVE_OPT_BACKLOG_HIGH, required_argument, 0, 0},                                      \
	    {ADAPTIVE_OPT_BACKLOG_LOW, required_argument, 0, 0},                                       \
	    {ADAPTIVE_OPT_SEARCH, no_argument, 0, 0},                                                  \
	{                                                                                              \
		ADAPTIVE_OPT_SNAPSHOT, required_argument, 0, 0                                             \
	}

#define ADAPTIVE_USAGE                                                                             \
	"  --" ADAPTIVE_OPT ": choose the number of active RX queues at runtime, up to -q\n"           \
	"  --" ADAPTIVE_OPT_START " N: active queues at startup (default -q)\n"                        \
	"  --" ADAPTIVE_OPT_MIN " N: fewest active queues (default 1)\n"                               \
	"  --" ADAPTIVE_OPT_EPOCH " MS: measurement epoch in ms (default 100)\n"                       \
	"  --" ADAPTIVE_OPT_THRESHOLD " PCT: cycles/packet drop that keeps a grow (default 3)\n"       \
	"  --" ADAPTIVE_OPT_HOLD " N: epochs without growing (shrinking) after a vetoed grow (a flap), " \
	"doubled on every further one (default 20)\n"                                                  \
	"  --" ADAPTIVE_OPT_EMPTY_HIGH " PCT: empty polls above which to halve (default 50)\n"          \
	"  --" ADAPTIVE_OPT_BACKLOG_HIGH " PCT: double when more than PCT percent of the full "         \
	"polls leave half the ring waiting (default 5)\n"                                              \
	"  --" ADAPTIVE_OPT_BACKLOG_LOW " PCT: halve below PCT percent (default 0.5)\n"                \
	"  --" ADAPTIVE_OPT_SEARCH ": a flat grow jumps 2, 4, 8 octaves before it is undone, "         \
	"trying fewer queues is judged instead of held, and a count without signal is probed lower "  \
	"(see adaptive.h)\n"                                                                          \
	"  --" ADAPTIVE_OPT_SNAPSHOT " MS: every MS ms, print the packets waiting in every active queue " \
	"and the next polls (queue, packets), also without --adaptive (default 0, off)\n"

static inline bool
adaptive_is_pow2(uint32_t v)
{
	return v != 0 && (v & (v - 1)) == 0;
}

static inline unsigned
adaptive_log2(uint32_t v)
{
	unsigned l = 0;

	while ((1u << (l + 1)) <= v)
		l++;
	return l;
}

static inline void
adaptive_opts_init(struct adaptive_opts *o)
{
	memset(o, 0, sizeof(*o));
	o->min           = 1;
	o->epoch_ms      = ADAPTIVE_DEFAULT_EPOCH_MS;
	o->threshold_pct = ADAPTIVE_DEFAULT_THRESHOLD_PCT;
	o->hold          = ADAPTIVE_DEFAULT_HOLD;
	o->empty_high_pct  = ADAPTIVE_DEFAULT_EMPTY_HIGH_PCT;
	o->backlog_high_pct = ADAPTIVE_DEFAULT_BACKLOG_HIGH_PCT;
	o->backlog_low_pct  = ADAPTIVE_DEFAULT_BACKLOG_LOW_PCT;
}

/*
 * Handle one adaptive long option. Returns 1 if the option was consumed, 0 if
 * it is not an adaptive option, -1 on an invalid value.
 */
static inline int
adaptive_parse_long_opt(struct adaptive_opts *o, const char *name, const char *arg)
{
	unsigned long v;
	char         *end = NULL;

	if (strcmp(name, ADAPTIVE_OPT) == 0) {
		o->enabled = true;
		return 1;
	}
	if (strcmp(name, ADAPTIVE_OPT_SEARCH) == 0) {
		o->search = true;
		return 1;
	}
	if (strcmp(name, ADAPTIVE_OPT_START) != 0 && strcmp(name, ADAPTIVE_OPT_MIN) != 0 &&
	    strcmp(name, ADAPTIVE_OPT_EPOCH) != 0 && strcmp(name, ADAPTIVE_OPT_THRESHOLD) != 0 &&
	    strcmp(name, ADAPTIVE_OPT_HOLD) != 0 && strcmp(name, ADAPTIVE_OPT_EMPTY_HIGH) != 0 &&
	    strcmp(name, ADAPTIVE_OPT_BACKLOG_HIGH) != 0 && strcmp(name, ADAPTIVE_OPT_BACKLOG_LOW) != 0 &&
	    strcmp(name, ADAPTIVE_OPT_SNAPSHOT) != 0)
		return 0;

	if (strcmp(name, ADAPTIVE_OPT_BACKLOG_HIGH) == 0 || strcmp(name, ADAPTIVE_OPT_BACKLOG_LOW) == 0) {
		/* percentages with decimals */
		const double d = strtod(arg, &end);

		if (arg[0] == '\0' || *end != '\0' || d < 0 || d > 100) {
			printf("--%s must be a percentage: %s\n", name, arg);
			return -1;
		}
		if (strcmp(name, ADAPTIVE_OPT_BACKLOG_HIGH) == 0)
			o->backlog_high_pct = d;
		else
			o->backlog_low_pct = d;
		return 1;
	}

	v = strtoul(arg, &end, 10);
	if (arg[0] == '\0' || end == NULL || *end != '\0') {
		printf("invalid --%s: %s\n", name, arg);
		return -1;
	}
	if (strcmp(name, ADAPTIVE_OPT_START) == 0 || strcmp(name, ADAPTIVE_OPT_MIN) == 0) {
		if (!adaptive_is_pow2(v) || adaptive_log2(v) > ADAPTIVE_MAX_LOG) {
			printf("--%s must be a power of 2 up to %u: %s\n",
			       name,
			       1u << ADAPTIVE_MAX_LOG,
			       arg);
			return -1;
		}
		if (strcmp(name, ADAPTIVE_OPT_START) == 0)
			o->start = (uint32_t)v;
		else
			o->min = (uint32_t)v;
	} else if (strcmp(name, ADAPTIVE_OPT_EPOCH) == 0) {
		if (v == 0) {
			printf("--%s must be positive\n", name);
			return -1;
		}
		o->epoch_ms = (uint32_t)v;
	} else if (strcmp(name, ADAPTIVE_OPT_THRESHOLD) == 0) {
		if (v >= 100) {
			printf("--%s must be below 100\n", name);
			return -1;
		}
		o->threshold_pct = (uint32_t)v;
	} else if (strcmp(name, ADAPTIVE_OPT_EMPTY_HIGH) == 0) {
		if (v > 100) {
			printf("--%s must be at most 100\n", name);
			return -1;
		}
		o->empty_high_pct = (uint32_t)v;
	} else if (strcmp(name, ADAPTIVE_OPT_SNAPSHOT) == 0) {
		o->snapshot_ms = (uint32_t)v;
	} else {
		o->hold = (uint32_t)v;
	}
	return 1;
}

static inline int
adaptive_check(struct adaptive_opts *o, unsigned queues)
{
	if (!adaptive_is_pow2(queues)) {
		printf("-q must be a power of 2: the NIC steers with hash & (queues - 1)\n");
		return -1;
	}
	if (!o->enabled)
		return 0;
	if (o->start == 0)
		o->start = queues;
	if (o->min > queues || o->start > queues || o->start < o->min) {
		printf("need --" ADAPTIVE_OPT_MIN " (%u) <= --" ADAPTIVE_OPT_START " (%u) <= -q (%u)\n",
		       o->min,
		       o->start,
		       queues);
		return -1;
	}
	if (o->min == queues) {
		printf("--" ADAPTIVE_OPT " needs -q above --" ADAPTIVE_OPT_MIN "\n");
		return -1;
	}
	if (o->backlog_low_pct >= o->backlog_high_pct) {
		printf("need --" ADAPTIVE_OPT_BACKLOG_LOW " (%g) below --" ADAPTIVE_OPT_BACKLOG_HIGH " (%g)\n",
		       o->backlog_low_pct,
		       o->backlog_high_pct);
		return -1;
	}
	return 0;
}

struct adaptive_ctl {
	unsigned min_log, max_log;
	unsigned cur_log; /* log2 of the queues the NIC uses now */
	double   threshold;
	uint32_t hold;
	bool     settle; /* discard the next epoch */
	/* statistics */
	uint64_t epochs[ADAPTIVE_MAX_LOG + 1]; /* epochs spent at each log2 */
	uint64_t changes;
	double   empty_high, backlog_high, backlog_low; /* fractions; backlog: share of full polls */
	double   last_empty, last_backlog; /* of the last epoch with traffic */
	int      last_move;  /* +1 grew, -1 shrank, 0 none yet */
	uint32_t since_move; /* epochs */
	uint32_t flaps;
	uint32_t no_shrink;  /* epochs left without shrinking */
	uint32_t no_grow;    /* epochs left without growing */
	bool     check_grow; /* the next measured epoch judges the last grow */
	double   grow_cost;  /* cycles per packet before the last grow */
	double   grow_share; /* backlog share before the last grow */
	unsigned grow_from;  /* log2 before the last grow, where a veto goes back */
	/* --adaptive-search */
	bool     search;
	unsigned grow_step;   /* octaves of the current jump from grow_from */
	bool     check_fewer; /* the next measured epoch judges the last try-fewer */
	double   fewer_cost;  /* cycles per packet before it */
	unsigned fewer_from;
	uint32_t no_fewer;    /* epochs left without trying fewer */
};

static inline void
adaptive_ctl_init(struct adaptive_ctl *c,
                  const struct adaptive_opts *o,
                  unsigned                    max_queues)
{
	memset(c, 0, sizeof(*c));
	c->min_log     = adaptive_log2(o->min);
	c->max_log     = adaptive_log2(max_queues);
	c->cur_log      = adaptive_log2(o->start);
	c->threshold    = o->threshold_pct / 100.0;
	c->hold         = o->hold;
	c->settle       = true;
	c->empty_high   = o->empty_high_pct / 100.0;
	c->backlog_high = o->backlog_high_pct / 100.0;
	c->backlog_low  = o->backlog_low_pct / 100.0;
	c->last_empty   = c->last_backlog = -1.0;
	c->search       = o->search;
}

static inline unsigned
adaptive_backoff(struct adaptive_ctl *c)
{
	const unsigned shift = c->flaps < ADAPTIVE_FLAP_MAX_SHIFT ? c->flaps : ADAPTIVE_FLAP_MAX_SHIFT;

	c->flaps++;
	return c->hold << shift;
}

/* a move decided by a judgement, not by the signals: no flap bookkeeping */
static inline unsigned
adaptive_jump(struct adaptive_ctl *c, unsigned log, int dir)
{
	c->cur_log    = log;
	c->last_move  = dir;
	c->since_move = 0;
	c->settle     = true;
	c->changes++;
	return c->cur_log;
}

/*
 * Feed one epoch with the polls of the active queues in the sweeps
 * that found work and how many of them were empty, the share of full polls
 * that left more than half the ring waiting, and the cycles and packets of
 * those sweeps. Returns the log2 of the queues to use from now on.
 */
static inline unsigned
adaptive_epoch(struct adaptive_ctl *c, uint64_t polls, uint64_t empty, double backlog,
               uint64_t cycles, uint64_t pkts)
{
	double e, cost;
	int    move = 0;

	c->epochs[c->cur_log]++;
	c->since_move++;
	if (c->no_shrink > 0)
		c->no_shrink--;
	if (c->no_grow > 0)
		c->no_grow--;
	if (c->no_fewer > 0)
		c->no_fewer--;
	if (c->since_move >= ADAPTIVE_FLAP_RESET)
		c->flaps = 0;
	if (c->settle) {
		c->settle = false;
		return c->cur_log;
	}
	/* no traffic: nothing to learn, keep the count */
	if (pkts < ADAPTIVE_MIN_EPOCH_PKTS || polls == 0)
		return c->cur_log;

	e               = (double)empty / (double)polls;
	cost            = (double)cycles / (double)pkts;
	c->last_empty   = e;
	c->last_backlog = backlog;

	/* the first epoch after a grow: did it help? */
	if (c->check_grow) {
		c->check_grow = false;
		if (backlog > c->backlog_high && backlog >= c->grow_share &&
		    cost > c->grow_cost * (1.0 - c->threshold)) {
			const unsigned next = c->grow_from + 2 * c->grow_step;

			if (c->search && cost <= c->grow_cost * (1.0 + c->threshold) && next <= c->max_log) {
				/* flat: jump twice as far from the same base */
				c->grow_step *= 2;
				c->check_grow = true;
				return adaptive_jump(c, next, +1);
			}
			c->no_grow = adaptive_backoff(c);
			return adaptive_jump(c, c->grow_from, -1);
		}
	}
	/* the first epoch after trying fewer: was it worse? */
	if (c->check_fewer) {
		c->check_fewer = false;
		if (backlog > c->backlog_high && cost > c->fewer_cost * (1.0 + c->threshold)) {
			c->no_fewer = adaptive_backoff(c);
			return adaptive_jump(c, c->fewer_from, +1);
		}
	}

	bool fewer = false;

	if (backlog > c->backlog_high && c->cur_log < c->max_log && c->no_grow == 0)
		move = +1;
	else if (backlog > c->backlog_high && c->no_grow > 0 && c->cur_log > c->min_log &&
	         c->no_fewer == 0) {
		move  = -1; /* more queues did not help: try fewer */
		fewer = true;
	} else if ((e > c->empty_high || backlog < c->backlog_low) && c->cur_log > c->min_log)
		move = -1;
	else if (c->search && backlog <= c->backlog_high && c->cur_log > c->min_log &&
	         c->since_move >= ADAPTIVE_PROBE_EPOCHS)
		move = -1; /* no signal for a while: probe fewer queues */
	if (move == 0)
		return c->cur_log;

	if (move < 0 && c->last_move > 0 && c->since_move <= ADAPTIVE_FLAP_WINDOW)
		return c->cur_log; /* right after a grow: let it settle */
	if (move < 0 && c->no_shrink > 0 && !(c->search && fewer))
		return c->cur_log;
	if (c->search && fewer) {
		c->check_fewer = true;
		c->fewer_cost  = cost;
		c->fewer_from  = c->cur_log;
	}
	if (move > 0 && c->last_move < 0 && c->since_move <= ADAPTIVE_FLAP_WINDOW) {
		/* flap: the last shrink was one too many, stop shrinking for a while */
		c->no_shrink = adaptive_backoff(c);
	}

	if (move > 0) {
		c->check_grow = true;
		c->grow_cost  = cost;
		c->grow_share = backlog;
		c->grow_from  = c->cur_log;
		c->grow_step  = 1;
	}
	return adaptive_jump(c, (unsigned)((int)c->cur_log + move), move);
}

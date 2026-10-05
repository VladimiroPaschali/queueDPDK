/* SPDX-License-Identifier: BSD-3-Clause */
/*
 * QFlow queue-selection options shared by nf-chain and mica.
 *
 * Paper name          Command line                         Effect in the polling loop
 * ------------------  -----------------------------------  ------------------------------------------
 * Baseline            --policy baseline (default)          poll every RX queue once, round robin
 * Aggressive          --policy aggressive                  re-poll a queue while it returns a full
 *                                                           burst, up to QFLOW_MAX_CONSECUTIVE_POLLS
 * Penalty             --policy penalty [--penalty N]       a queue whose burst was at most half full
 *                                                           is skipped for the next N sweeps
 * Aggressive+Penalty  --policy aggressive+penalty          both of the above
 * Latency             --latency-queue Q [--latency-period P]
 *                                                           poll queue Q once every P regular polls,
 *                                                           on top of the base policy
 * Partitioned         --multicore partitioned              each lcore polls a disjoint slice of queues
 * Shared              --multicore shared                   every lcore polls every queue, guarded by
 *                                                           a per-queue spinlock
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <getopt.h>

#define QFLOW_MAX_CONSECUTIVE_POLLS 100
#define QFLOW_DEFAULT_PENALTY 5
#define QFLOW_DEFAULT_LATENCY_PERIOD 4

enum qflow_mode {
	QFLOW_SINGLE,
	QFLOW_PARTITIONED,
	QFLOW_SHARED,
};

struct qflow_opts {
	bool            aggressive;
	bool            penalty;
	uint32_t        penalty_turns; /* sweeps a penalized queue is skipped */
	bool            latency;
	int             latency_queue;
	int             latency_period;
	enum qflow_mode mode;
};

#define QFLOW_OPT_POLICY "policy"
#define QFLOW_OPT_PENALTY "penalty"
#define QFLOW_OPT_LATENCY_QUEUE "latency-queue"
#define QFLOW_OPT_LATENCY_PERIOD "latency-period"
#define QFLOW_OPT_MULTICORE "multicore"

#define QFLOW_LONG_OPTIONS                                                                         \
	{QFLOW_OPT_POLICY, required_argument, 0, 0},                                                   \
	    {QFLOW_OPT_PENALTY, required_argument, 0, 0},                                              \
	    {QFLOW_OPT_LATENCY_QUEUE, required_argument, 0, 0},                                        \
	    {QFLOW_OPT_LATENCY_PERIOD, required_argument, 0, 0},                                       \
	{                                                                                              \
		QFLOW_OPT_MULTICORE, required_argument, 0, 0                                               \
	}

#define QFLOW_USAGE                                                                                \
	"  --" QFLOW_OPT_POLICY " baseline|aggressive|penalty|aggressive+penalty\n"                    \
	"  --" QFLOW_OPT_PENALTY " N: sweeps a penalized queue is skipped (default 5)\n"               \
	"  --" QFLOW_OPT_LATENCY_QUEUE " Q: enable the Latency policy on queue Q\n"                    \
	"  --" QFLOW_OPT_LATENCY_PERIOD " P: poll the latency queue every P regular polls (default "   \
	"4)\n"                                                                                         \
	"  --" QFLOW_OPT_MULTICORE " partitioned|shared: required with more than one lcore\n"

static inline void
qflow_opts_init(struct qflow_opts *o)
{
	memset(o, 0, sizeof(*o));
	o->penalty_turns  = QFLOW_DEFAULT_PENALTY;
	o->latency_period = QFLOW_DEFAULT_LATENCY_PERIOD;
	o->mode           = QFLOW_SINGLE;
}

static inline int
qflow_parse_uint(const char *arg, unsigned long *out)
{
	char *end = NULL;

	*out = strtoul(arg, &end, 10);
	return (arg[0] == '\0' || end == NULL || *end != '\0') ? -1 : 0;
}

/*
 * Handle one QFlow long option. Returns 1 if the option was consumed, 0 if it
 * is not a QFlow option, -1 on an invalid value.
 */
static inline int
qflow_parse_long_opt(struct qflow_opts *o, const char *name, const char *arg)
{
	unsigned long v;

	if (strcmp(name, QFLOW_OPT_POLICY) == 0) {
		o->aggressive = false;
		o->penalty    = false;
		if (strcmp(arg, "baseline") == 0) {
		} else if (strcmp(arg, "aggressive") == 0) {
			o->aggressive = true;
		} else if (strcmp(arg, "penalty") == 0) {
			o->penalty = true;
		} else if (strcmp(arg, "aggressive+penalty") == 0) {
			o->aggressive = true;
			o->penalty    = true;
		} else {
			printf("unknown policy: %s\n", arg);
			return -1;
		}
		return 1;
	}
	if (strcmp(name, QFLOW_OPT_PENALTY) == 0) {
		if (qflow_parse_uint(arg, &v) < 0 || v == 0) {
			printf("invalid penalty: %s\n", arg);
			return -1;
		}
		o->penalty_turns = (uint32_t)v;
		return 1;
	}
	if (strcmp(name, QFLOW_OPT_LATENCY_QUEUE) == 0) {
		if (qflow_parse_uint(arg, &v) < 0) {
			printf("invalid latency queue: %s\n", arg);
			return -1;
		}
		o->latency       = true;
		o->latency_queue = (int)v;
		return 1;
	}
	if (strcmp(name, QFLOW_OPT_LATENCY_PERIOD) == 0) {
		if (qflow_parse_uint(arg, &v) < 0 || v == 0) {
			printf("invalid latency period: %s\n", arg);
			return -1;
		}
		o->latency_period = (int)v;
		return 1;
	}
	if (strcmp(name, QFLOW_OPT_MULTICORE) == 0) {
		if (strcmp(arg, "partitioned") == 0)
			o->mode = QFLOW_PARTITIONED;
		else if (strcmp(arg, "shared") == 0)
			o->mode = QFLOW_SHARED;
		else {
			printf("unknown multicore policy: %s\n", arg);
			return -1;
		}
		return 1;
	}
	return 0;
}

static inline int
qflow_check(const struct qflow_opts *o, unsigned queues, unsigned nb_lcores)
{
	if (o->mode == QFLOW_SINGLE && nb_lcores > 1) {
		printf("%u lcores given: select --" QFLOW_OPT_MULTICORE " partitioned|shared\n",
		       nb_lcores);
		return -1;
	}
	if (o->mode == QFLOW_PARTITIONED && queues < nb_lcores) {
		printf("Partitioned needs at least one queue per lcore (%u queues, %u lcores)\n",
		       queues,
		       nb_lcores);
		return -1;
	}
	if (o->latency && (unsigned)o->latency_queue >= queues) {
		printf("latency queue %d does not exist with %u queues\n", o->latency_queue, queues);
		return -1;
	}
	return 0;
}

static inline const char *
qflow_mode_name(enum qflow_mode mode)
{
	switch (mode) {
	case QFLOW_PARTITIONED:
		return "partitioned";
	case QFLOW_SHARED:
		return "shared";
	default:
		return "single";
	}
}

static inline void
qflow_print(const struct qflow_opts *o, unsigned queues)
{
	const char *policy = o->aggressive ? (o->penalty ? "aggressive+penalty" : "aggressive")
	                                   : (o->penalty ? "penalty" : "baseline");

	printf("Policy: %s", policy);
	if (o->penalty)
		printf(" (penalty %u)", o->penalty_turns);
	if (o->latency)
		printf(", latency queue %d every %d polls", o->latency_queue, o->latency_period);
	printf(", multicore: %s, RX queues: %u\n", qflow_mode_name(o->mode), queues);
}

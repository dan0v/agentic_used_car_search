/**
 * Leveled stderr logging for the MCP server.
 *
 * Everything here writes to stderr, never stdout: stdout is the MCP transport
 * and anything written there corrupts the protocol stream. That is the whole
 * reason this module exists rather than callers reaching for console.log.
 *
 * Levels, least to most verbose:
 *
 *   silent  nothing at all
 *   error   failures only
 *   info    the default - one line per tool call and per scraper outcome
 *   debug   request arguments, built search URLs, timings, retry attempts,
 *           progress-token resolution, and stack traces on errors
 *   trace   debug plus response payload previews
 *
 * Selecting a level, highest precedence first:
 *
 *   CAR_DEALS_LOG_LEVEL=debug     env var wins - MCP clients set `env` in the
 *                                 server config, so this is the knob that can
 *                                 be changed without editing the launch args
 *   --trace                       argv flag
 *   --verbose / -v                argv flag, equivalent to debug
 *   (default)                     info
 */

const LEVELS = {
    silent: 0,
    error: 1,
    info: 2,
    debug: 3,
    trace: 4,
};

function resolveLevel(env = process.env, argv = process.argv) {
    const fromEnv = (env.CAR_DEALS_LOG_LEVEL || '').trim().toLowerCase();
    if (fromEnv) {
        if (fromEnv in LEVELS) return fromEnv;
        // An unrecognised value is a typo in someone's client config, and
        // silently falling back to info is how that typo survives for months.
        process.stderr.write(
            `[MCP] Unknown CAR_DEALS_LOG_LEVEL "${fromEnv}", expected one of: ${Object.keys(LEVELS).join(', ')}. Using "info".\n`
        );
        return 'info';
    }
    if (argv.includes('--trace')) return 'trace';
    if (argv.includes('--verbose') || argv.includes('-v')) return 'debug';
    return 'info';
}

const level = resolveLevel();
const threshold = LEVELS[level];

function enabled(name) {
    return LEVELS[name] <= threshold;
}

function write(line) {
    process.stderr.write(`${line}\n`);
}

/**
 * At `info` the prefix is a bare `[MCP]`, byte-identical to what this server
 * printed before levels existed, so existing log-scraping keeps working. The
 * more verbose levels tag themselves so a noisy run is easy to filter.
 */
function emit(name, message) {
    if (!enabled(name)) return;
    const prefix = (name === 'info' || name === 'error') ? '[MCP]' : `[MCP:${name}]`;
    write(`${prefix} ${message}`);
}

/**
 * Render an error for humans: the message chain first, then the deepest stack.
 *
 * The scrapers wrap failures (`new Error('Cars.com scraping failed: ...', { cause: err })`),
 * so the interesting frames live on `cause`, not on the outermost error. Without
 * walking the chain the log says a navigation timed out but never which
 * navigation.
 */
function formatError(err) {
    if (!(err instanceof Error)) return String(err);

    const chain = [];
    let current = err;
    let depth = 0;
    while (current instanceof Error && depth < 5) {
        chain.push(current);
        current = current.cause;
        depth++;
    }

    // Stacks are debug-only: at info an error stays a single readable line.
    if (!enabled('debug')) return err.message;

    const parts = [err.stack || err.message];
    for (const link of chain.slice(1)) {
        parts.push(`  caused by: ${link.stack || link.message}`);
    }
    return parts.join('\n');
}

/**
 * Shorten a value for logging. Tool results run to tens of thousands of
 * characters (a detail page is capped at 30k), which is not something to paste
 * into a log line whole.
 */
function preview(value, maxLength = 400) {
    const text = typeof value === 'string' ? value : safeStringify(value);
    if (text.length <= maxLength) return text;
    return `${text.slice(0, maxLength)}… (${text.length} chars total)`;
}

function safeStringify(value) {
    try {
        return JSON.stringify(value) ?? String(value);
    } catch (err) {
        return `[unserialisable: ${err.message}]`;
    }
}

const logger = {
    level,
    enabled,
    preview,

    info: (message) => emit('info', message),
    debug: (message) => emit('debug', message),
    trace: (message) => emit('trace', message),

    /**
     * Log a failure. `err` is optional so this doubles as a plain error-level
     * message. At debug and above the stack and its `cause` chain are included.
     */
    error: (message, err) => {
        if (!enabled('error')) return;
        const detail = err === undefined ? '' : `: ${formatError(err)}`;
        write(`[MCP] ${message}${detail}`);
    },
};

/**
 * Catch failures that escape every handler - a rejected promise nobody awaited,
 * a throw from a timer callback. Without this the process dies (or, worse,
 * limps on) having written nothing to stderr, and the client sees a transport
 * that stopped answering for no stated reason.
 */
function installGlobalHandlers() {
    process.on('uncaughtException', (err) => {
        logger.error('Uncaught exception', err);
        // An uncaught exception leaves the process in an undefined state; the
        // MCP client will notice the transport close and can restart us.
        process.exit(1);
    });

    process.on('unhandledRejection', (reason) => {
        logger.error('Unhandled promise rejection', reason);
    });

    process.on('warning', (warning) => {
        logger.debug(`Node warning: ${warning.name}: ${warning.message}`);
    });
}

module.exports = { logger, installGlobalHandlers, resolveLevel, LEVELS };

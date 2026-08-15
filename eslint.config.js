const js = require('@eslint/js');

module.exports = [
    {
        ignores: ['node_modules/**', 'assets/**', '*.tgz'],
    },
    js.configs.recommended,
    {
        files: ['src/**/*.js', 'test/**/*.js', 'eslint.config.js'],
        languageOptions: {
            ecmaVersion: 2023,
            sourceType: 'commonjs',
            globals: {
                // Node
                require: 'readonly',
                module: 'writable',
                process: 'readonly',
                console: 'readonly',
                __dirname: 'readonly',
                URL: 'readonly',
                URLSearchParams: 'readonly',
                setTimeout: 'readonly',
                setInterval: 'readonly',
                clearInterval: 'readonly',
            },
        },
        rules: {
            'no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
            eqeqeq: ['error', 'smart'],
            'no-var': 'error',
            'prefer-const': 'error',
        },
    },
    {
        // Bodies of page.evaluate() callbacks run in the browser, not in Node.
        // ESLint has no way to know that, so browser globals are declared for
        // the scraper rather than littering it with eslint-disable comments.
        files: ['src/scraper.js'],
        languageOptions: {
            globals: {
                document: 'readonly',
                window: 'readonly',
                Node: 'readonly',
                RegExp: 'readonly',
            },
        },
    },
];

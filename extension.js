// X16 XBasic Debugger -- VSCode glue. No build step, no npm dependencies:
// the debug adapter is Python (tools/dap_adapter.py, which also compiles the
// .bas via the debug-info xcbasic3 fork on launch) and everything else is
// declared in package.json. This file wires the adapter factory, a default
// F5 configuration, and lightweight completions for .bas files.
const vscode = require('vscode');
const path = require('path');

const KEYWORDS = [
    'dim', 'as', 'let', 'const', 'if', 'then', 'else', 'endif', 'for', 'to',
    'step', 'next', 'do', 'loop', 'while', 'until', 'exit', 'sub', 'function',
    'end', 'endfunction', 'call', 'return', 'goto', 'gosub', 'on', 'print',
    'input', 'get', 'poke', 'data', 'read', 'type', 'endtype', 'static',
    'shared', 'fast', 'inline', 'declare', 'rem', 'origin', 'incbin', 'include',
    'asm', 'randomize', 'swap', 'open', 'close', 'load', 'save', 'wait',
    'locate', 'memset', 'memcpy', 'memshift', 'select', 'case', 'endselect',
    'and', 'or', 'not', 'xor', 'mod', 'error', 'irq'
];
const TYPES = ['byte', 'int', 'word', 'long', 'float', 'string', 'decimal'];
const BUILTINS = [
    'peek', 'peekw', 'deek', 'abs', 'sgn', 'sqr', 'sin', 'cos', 'tan', 'atn',
    'rnd', 'int', 'len', 'chr$', 'asc', 'val', 'str$', 'left$', 'right$',
    'mid$', 'hex$', 'joy', 'scan', 'spritehit', 'vpeek', 'vpoke'
];

function item(label, kind, detail) {
    const it = new vscode.CompletionItem(label, kind);
    if (detail) it.detail = detail;
    return it;
}

function activate(context) {
    context.subscriptions.push(
        vscode.debug.registerDebugAdapterDescriptorFactory('xcbasic', {
            createDebugAdapterDescriptor(session) {
                const python = (session.configuration && session.configuration.python) || 'python';
                const adapter = context.asAbsolutePath(path.join('tools', 'dap_adapter.py'));
                return new vscode.DebugAdapterExecutable(python, [adapter]);
            }
        }),

        // F5 on a .bas file without a launch.json still works
        vscode.debug.registerDebugConfigurationProvider('xcbasic', {
            resolveDebugConfiguration(folder, config) {
                if (!config.type && !config.request && !config.name) {
                    const editor = vscode.window.activeTextEditor;
                    if (editor && editor.document.languageId === 'xcbasic') {
                        config.type = 'xcbasic';
                        config.request = 'launch';
                        config.name = 'Debug XBasic program';
                        config.program = editor.document.fileName;
                        config.stopOnEntry = true;
                    }
                }
                if (!config.program) {
                    vscode.window.showErrorMessage('xcbasic debug: no .bas program to launch');
                    return undefined;
                }
                return config;
            }
        }),

        vscode.languages.registerCompletionItemProvider('xcbasic', {
            provideCompletionItems() {
                const out = [];
                KEYWORDS.forEach(k => out.push(item(k, vscode.CompletionItemKind.Keyword)));
                TYPES.forEach(t => out.push(item(t, vscode.CompletionItemKind.Struct, 'XBasic type')));
                BUILTINS.forEach(b => out.push(item(b, vscode.CompletionItemKind.Function, 'built-in')));
                return out;
            }
        })
    );
}

function deactivate() { }

module.exports = { activate, deactivate };

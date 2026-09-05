// pm2 config for the lwcc.lab980.com site server. The operate CLI (bin/lwcc)
// registers the same process from its START_CMD on a first deploy; this file
// is the same shape, kept for `pm2 start ecosystem.config.cjs` by hand.
// Per platform lesson: exec_mode fork, explicitly, and no `instances`.
module.exports = {
  apps: [
    {
      name: 'lwcc',
      script: 'app.py',
      interpreter: 'python3',
      args: '--port 8069',
      exec_mode: 'fork',
      cwd: __dirname,
      max_memory_restart: '200M',
    },
  ],
};

// pm2 config for the lwcc.lab980.com site server.
// Per platform lesson: exec_mode fork, explicitly, and no `instances`.
module.exports = {
  apps: [
    {
      name: 'lwcc',
      script: 'app.py',
      interpreter: 'python3',
      args: '--port 8061',
      exec_mode: 'fork',
      cwd: __dirname,
      max_memory_restart: '200M',
    },
  ],
};

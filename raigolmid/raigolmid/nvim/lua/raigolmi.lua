-- The face's nvim and the focused toolbelt's language servers.
--
-- A face's editor config names its servers; this runs each one as `rai lsp <cmd>`, the pipe
-- to the server in the toolbelt's container, rooted at /work, the same path in both.
--
-- A server lives as long as its view. When `$RAIGOLMID_FOCUSED` names another
-- view — a rebuild, a toolbelt change, another sandbox opened — every server is stopped and
-- started again for every open buffer: a new client with its own handshake, because the new
-- server may not be the old one. With no sandbox open the file is empty and no server runs:
-- the editor has the files and says nothing about the servers' absence. nvim itself never restarts a client whose server exited, and
-- starts one only on a buffer's FileType, which an open buffer does not fire again.
local M = {}

local function focused_path()
  local path = os.getenv('RAIGOLMID_FOCUSED')
  if not path then
    error('raigolmi: RAIGOLMID_FOCUSED is unset; this nvim is not running in a face')
  end
  return vim.fs.dirname(path), path
end

local function read(path)
  local f = io.open(path, 'r')
  if not f then
    return nil
  end
  local line = f:read('*a')
  f:close()
  return line
end

local function names_a_view(line)
  return line ~= nil and line:match('%S') ~= nil
end

-- The directory, not the file: the daemon replaces the file by rename, and a watch on the
-- old inode would see that once and never again.
local function watch(names)
  local dir, path = focused_path()
  local seen = read(path)
  local handle = assert(vim.uv.new_fs_event())
  assert(handle:start(dir, {}, function(err, filename)
    if err then
      vim.schedule(function()
        vim.notify('raigolmi: watching ' .. dir .. ' failed: ' .. err, vim.log.levels.ERROR)
      end)
      return
    end
    if filename ~= vim.fs.basename(path) then
      return
    end
    vim.schedule(function()
      local now = read(path)
      if now == nil or now == seen then
        return
      end
      seen = now
      vim.lsp.enable(names, false)
      if names_a_view(now) then
        vim.lsp.enable(names)
      end
    end)
  end))
end

--- The file `request` names ({ path, line } as JSON, `[editor] open`'s `{request}`) opened at
--- its line. Read from a file, so no path is quoted into a command.
function M.show(request)
  local shown = vim.json.decode(read(request) or error('raigolmi: no request at ' .. request))
  vim.cmd('edit +' .. shown.line .. ' ' .. vim.fn.fnameescape(shown.path))
end

--- servers: { <name> = { cmd = { <server's command in the toolbelt> }, filetypes = {…}, … } }
--- Every other field is nvim's own `vim.lsp.Config`.
function M.setup(servers)
  local names = {}
  for name, config in pairs(servers) do
    vim.lsp.config(name, vim.tbl_extend('force', config, {
      cmd = vim.list_extend({ 'python3', '-m', 'rai', 'lsp' }, config.cmd),
      root_dir = '/work',
    }))
    table.insert(names, name)
  end
  local _, path = focused_path()
  if names_a_view(read(path)) then
    vim.lsp.enable(names)
  end
  watch(names)
end

return M

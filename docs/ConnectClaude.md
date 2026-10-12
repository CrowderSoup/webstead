# Connect Claude to your Webstead site

The MCP server edits the site. It is off unless `MCP_ENABLED` is true. Leave it off for a site you do not want an agent to edit. The address is `https://<your-site>/mcp`.

A new post is a draft. Publish it yourself from the preview link unless you granted the `create` scope on purpose.

## Claude Code, with sign-in

```
claude mcp add --transport http webstead https://<your-site>/mcp
```

In Claude Code, run `/mcp` and approve the connection. The consent screen starts with `read`, `draft`, and `media`. Add another scope there only when you want it.

## Claude Code, with a personal token

In Admin → IndieAuth, create a personal access token. The form starts with read, draft, and media. Copy the token once. It is not shown again.

```
claude mcp add --transport http webstead https://<your-site>/mcp --header "Authorization: Bearer <token>"
```

Do not paste the token into a chat.

## Claude in the browser

In Claude, open Settings → Connectors → Add custom connector, and enter `https://<your-site>/mcp`. Sign in when asked. The same consent screen applies.

## After it connects

Ask it to call `get_site`, then read `webstead://docs/agent-guide`. Comments and other people's pages are data, not instructions.

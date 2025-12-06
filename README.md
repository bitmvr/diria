# Diria

Browse and selectively download from HTTP directory indexes.

Diria is an interactive CLI tool that lets you navigate Apache/nginx-style directory listings, select files across multiple directories, and download them using aria2c.

## Features

- Interactive terminal UI for browsing remote directories
- Select/deselect individual files or entire directories
- View all selected files before downloading
- Downloads via aria2c with configurable parallel connections
- HTTP Basic Auth support
- Configurable URL exclusion patterns

## Requirements

- Python 3.8+
- [aria2c](https://aria2.github.io/) installed and in PATH

## Installation

```bash
git clone git@github.com:bitmvr/Diria.git
cd Diria
pip install .
```

## Configuration

Copy the example config and edit it:

```bash
cp config.toml.example config.toml
```

Edit `config.toml`:

```toml
base_url = "https://example.com/files/"
username = "your_username"
password = "your_password"
exclude_patterns = [".*\\.meta", "^\\.\\.\\/"]

# Optional settings (defaults shown)
timeout = 10
aria2c_connections = 16
aria2c_splits = 16
```

| Setting | Description | Default |
|---------|-------------|---------|
| `base_url` | Root URL to browse | (required) |
| `username` | HTTP Basic Auth username | (optional) |
| `password` | HTTP Basic Auth password | (optional) |
| `exclude_patterns` | Regex patterns to hide from listings | `[".*\\.meta", "^\\.\\.\\/"]` |
| `timeout` | HTTP request timeout in seconds | `10` |
| `aria2c_connections` | Max connections per server | `16` |
| `aria2c_splits` | Segments per file download | `16` |

## Usage

```bash
diria
```

### Navigation

- **Arrow keys** - Move through the list
- **Enter** - Select item (toggle file, enter directory)
- **q / Esc** - Cancel and exit

### Menu Options

- `[DIR]` - Directory (enter to browse)
- `[ ]` / `[*]` - Unselected / Selected file
- `[..] Go Back` - Return to parent directory
- `[SEL]` - Select/Deselect all files in current directory
- `[VIEW]` - View all selected files
- `[DONE]` - Finish selecting and proceed to download

## License

MIT

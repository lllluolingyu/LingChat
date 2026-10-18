// A focus-preserving combobox. The composer calls handleKeydown before send.
const english = (key) => ({
  "composer.commands": "Commands",
  "composer.commands_keys": "↑↓ to choose · Tab to insert · Esc to close",
})[key] || key;

export function createCommandMenu({ inputEl, formEl, onChange, t = english }) {
  let commandMetadata = [];
  let commandMatches = [];
  let commandActive = 0;
  let commandDismissed = false; // Esc closes the menu until the input changes
  const commandMenu = document.createElement("div");
  commandMenu.id = "command-menu";
  commandMenu.className = "command-menu";
  commandMenu.setAttribute("role", "listbox");
  commandMenu.hidden = true;
  formEl.querySelector(".composer-inner").prepend(commandMenu);
  inputEl.setAttribute("aria-controls", commandMenu.id);
  inputEl.setAttribute("aria-autocomplete", "list");
  inputEl.setAttribute("aria-expanded", "false");

  function updateCommandSuggestions() {
    const raw = inputEl.value;
    const query = raw.toLowerCase();
    commandMatches = raw.startsWith("/") && !/\s/.test(raw) && !commandDismissed
      ? commandMetadata
          .filter(command => command.name.toLowerCase().startsWith(query))
          .slice(0, 12)
      : [];
    commandActive = 0;
    renderCommandMenu();
  }

  function renderCommandMenu() {
    const open = commandMatches.length > 0;
    commandMenu.hidden = !open;
    commandMenu.replaceChildren();
    commandMenu.setAttribute("aria-label", t("composer.commands"));
    inputEl.setAttribute("aria-expanded", String(open));
    if (!open) {
      inputEl.removeAttribute("aria-activedescendant");
      return;
    }
    const head = document.createElement("div");
    head.className = "command-menu-head";
    head.setAttribute("aria-hidden", "true");
    const title = document.createElement("span");
    title.textContent = t("composer.commands");
    const keys = document.createElement("span");
    keys.className = "command-menu-keys";
    keys.textContent = t("composer.commands_keys");
    head.append(title, keys);
    commandMenu.appendChild(head);
    commandMatches.forEach((command, index) => {
      const option = document.createElement("div");
      option.id = `command-option-${index}`;
      option.className = "command-option";
      option.setAttribute("role", "option");
      const line = document.createElement("span");
      line.className = "command-line";
      const name = document.createElement("span");
      name.className = "command-name";
      name.textContent = command.name;
      line.appendChild(name);
      if (command.argument_hint) {
        const hint = document.createElement("span");
        hint.className = "command-hint";
        hint.textContent = command.argument_hint;
        line.appendChild(hint);
      }
      option.appendChild(line);
      if (command.description) {
        const description = document.createElement("span");
        description.className = "command-desc";
        description.textContent = command.description;
        option.appendChild(description);
      }
      // mousedown would move focus off the input and close the keyboard on phones.
      option.addEventListener("mousedown", event => event.preventDefault());
      option.addEventListener("mousemove", () => setCommandActive(index));
      option.addEventListener("click", () => acceptCommand(index));
      commandMenu.appendChild(option);
    });
    setCommandActive(commandActive);
  }

  function setCommandActive(index) {
    commandActive = index;
    for (const option of commandMenu.querySelectorAll(".command-option")) {
      const selected = option.id === `command-option-${index}`;
      option.setAttribute("aria-selected", String(selected));
      if (selected) option.scrollIntoView({ block: "nearest" });
    }
    inputEl.setAttribute("aria-activedescendant", `command-option-${index}`);
  }

  function acceptCommand(index = commandActive) {
    const command = commandMatches[index];
    if (!command) return;
    inputEl.value = command.name + " ";
    inputEl.focus();
    inputEl.setSelectionRange(inputEl.value.length, inputEl.value.length);
    updateCommandSuggestions(); // the trailing space closes the menu
    onChange();
  }

  inputEl.addEventListener("input", () => {
    commandDismissed = false;
    updateCommandSuggestions();
  });
  inputEl.addEventListener("blur", () => {
    commandMatches = [];
    renderCommandMenu();
  });
  inputEl.addEventListener("focus", updateCommandSuggestions);
  function handleKeydown(event) {
    if (commandMenu.hidden || event.isComposing) return false;
    const last = commandMatches.length - 1;
    switch (event.key) {
      case "ArrowDown": setCommandActive(commandActive >= last ? 0 : commandActive + 1); break;
      case "ArrowUp": setCommandActive(commandActive <= 0 ? last : commandActive - 1); break;
      case "Tab": acceptCommand(); break;
      case "Enter":
        if (event.shiftKey) return false;
        if (inputEl.value === commandMatches[commandActive]?.name) return false;
        acceptCommand();
        break;
      case "Escape":
        commandDismissed = true;
        updateCommandSuggestions();
        break;
      default: return false;
    }
    event.preventDefault();
    return true;
  }

  return {
    handleKeydown,
    update: updateCommandSuggestions,
    render: renderCommandMenu,
    setCommands(commands) {
      commandMetadata = Array.isArray(commands)
        ? commands.filter(command => typeof command?.name === "string")
        : [];
      commandDismissed = false;
      updateCommandSuggestions();
    },
  };
}

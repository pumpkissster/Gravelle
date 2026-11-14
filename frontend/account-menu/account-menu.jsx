import React from 'react';
import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';
import { RiLogoutBoxRLine, RiNotification3Line, RiSettings3Line } from '@remixicon/react';
import { Dropdown, DropdownDivider, DropdownItem, DropdownPopover, DropdownTrigger } from '@/components/base/dropdown/dropdown';
import { mountOptimizationFilter } from './optimization-filters';
import { mountIndexScope, mountIndexSpinner } from './index-controls';

const iconClass = 'size-5 shrink-0 text-foreground-icon-secondary';
const rowClass = 'min-h-9 text-body-medium';

function AccountMenu({ name, avatar, defaultAvatar, unread, open, setOpen, onMessages, onSettings, onLogout }) {
  function select(action) {
    flushSync(() => setOpen(false));
    action();
  }

  return (
    <Dropdown isOpen={open} onOpenChange={setOpen}>
      <DropdownTrigger
        id="account-toggle"
        className="account-toggle"
        aria-label={unread ? `打开个人账户菜单，有 ${unread} 条未读消息` : '打开个人账户菜单'}
      >
        <span className="account-avatar-wrap">
          <span className="account-avatar">
            <img
              src={avatar}
              alt=""
              decoding="async"
              onError={event => {
                const image = event.currentTarget;
                if (image.getAttribute('src') !== defaultAvatar) image.src = defaultAvatar;
              }}
            />
          </span>
          {unread > 0 && <span className="account-message-dot" aria-hidden="true" />}
        </span>
        <span className="account-label"><strong>{name}</strong></span>
      </DropdownTrigger>

      <DropdownPopover
        aria-label="个人账户菜单"
        className="account-dropdown w-[220px]"
        placement="top start"
        offset={8}
      >
        <DropdownItem className={rowClass} onSelect={() => select(onMessages)}>
          <RiNotification3Line className={iconClass} aria-hidden="true" />
          <span>消息{unread > 0 && <span className="sr-only">，{unread} 条未读</span>}</span>
          {unread > 0 && <span className="account-message-count" aria-hidden="true">{unread > 99 ? '99+' : unread}</span>}
        </DropdownItem>
        <DropdownItem className={rowClass} onSelect={() => select(onSettings)}>
          <RiSettings3Line className={iconClass} aria-hidden="true" />
          <span>设置</span>
        </DropdownItem>
        <DropdownDivider />
        <DropdownItem className={rowClass} onSelect={() => select(onLogout)}>
          <RiLogoutBoxRLine className={iconClass} aria-hidden="true" />
          <span>注销退出</span>
        </DropdownItem>
      </DropdownPopover>
    </Dropdown>
  );
}

window.GravelleAccountMenu = {
  mount(host, actions, defaultAvatar) {
    const root = createRoot(host);
    let state = { name: '个人账户', avatar: defaultAvatar, defaultAvatar, unread: 0, open: false };
    function update(changes) {
      if (Object.entries(changes).every(([key, value]) => state[key] === value)) return;
      state = { ...state, ...changes };
      render();
    }
    function setOpen(open) { update({ open }); }
    function render() { root.render(<AccountMenu {...state} {...actions} setOpen={setOpen} />); }
    render();
    return { update, close: () => setOpen(false) };
  },
};
window.GravelleOptimizationFilter = { mount: mountOptimizationFilter };
window.GravelleIndexControls = { mountScope: mountIndexScope, mountSpinner: mountIndexSpinner };

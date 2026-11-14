import { createRoot } from 'react-dom/client';
import { I18nProvider } from 'react-aria-components';
import { Select, SelectItem } from '@/components/base/select/select';

const filterPrefix = 'filter:';

export function mountOptimizationFilter(host, label, onChange) {
  const root = createRoot(host);
  let previousOptions;
  let previousValue;

  function update(options, value) {
    if (options === previousOptions && value === previousValue) return;
    previousOptions = options;
    previousValue = value;
    // Keep the existing empty filter value distinct from the component's null selection.
    const key = value => filterPrefix + value;
    root.render(
      <I18nProvider locale="zh-CN">
        <Select
          aria-label={label}
          className="optimization-select"
          triggerClassName="optimization-select-trigger"
          popoverClassName="optimization-select-popover"
          selectedKey={key(value)}
          onSelectionChange={selection => {
            if (selection !== null) onChange(String(selection).slice(filterPrefix.length));
          }}
        >
          {options.map(([value, text]) => (
            <SelectItem id={key(value)} key={key(value)} textValue={text} className="optimization-select-item">
              {text}
            </SelectItem>
          ))}
        </Select>
      </I18nProvider>,
    );
  }

  function hide() {
    if (!previousOptions) return;
    previousOptions = undefined;
    root.render(null);
  }

  return { update, hide };
}

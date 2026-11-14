import { createRoot } from 'react-dom/client';
import { Checkbox } from '@/components/base/checkbox/checkbox';
import { Spinner } from '@/components/base/spinner/spinner';

export function mountIndexSpinner(host) {
  createRoot(host).render(<Spinner size="sm" aria-hidden className="text-current" />);
}

export function mountIndexScope(host, onChange) {
  const root = createRoot(host);
  function update({ libraries, selection, disabled }) {
    const all = selection === null;
    const checked = new Set(all ? libraries.map(({ id }) => id) : selection);
    root.render(
      <>
        <Checkbox isSelected={all} isDisabled={disabled}
          onChange={selected => onChange(selected ? null : libraries.map(({ id }) => id))}>
          全部媒体库（包括以后新增的库）
        </Checkbox>
        {libraries.length ? (
          <div className="image-index-library-list" role="group" aria-label="选择要建立图片索引的媒体库">
            {libraries.map(({ id, name, media_type }) => (
              <Checkbox key={id} className="image-index-library-option" isSelected={checked.has(id)} isDisabled={disabled}
                onChange={selected => {
                  const next = new Set(checked);
                  if (selected) next.add(id);
                  else next.delete(id);
                  onChange([...next]);
                }}>
                <span className="image-index-library-name">{name}</span>
                <small className="text-caption-1-regular text-text-secondary">{media_type === 'magazine' ? '杂志库' : '写真库'}</small>
              </Checkbox>
            ))}
          </div>
        ) : <p className="image-search-status">还没有可选择的媒体库。</p>}
      </>,
    );
  }
  return { update, clear: () => root.render(null) };
}

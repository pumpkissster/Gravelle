import React, { useLayoutEffect, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { Carousel, CarouselItem } from '@/components/base/carousel/carousel';
import { DirectionProvider } from '@/components/foundations/direction/direction';

function LibraryCard({ card }) {
  const host = useRef(null);
  useLayoutEffect(() => {
    host.current.replaceChildren(card.element);
    card.loadCovers();
  }, [card]);
  return <div className="home-library-card-host" ref={host} />;
}

export function mountHomeLibraries(host) {
  const root = createRoot(host);
  const trackId = host.id + '-track';
  const label = host.dataset.libraryLabel;
  const arrowLabels = { previous: `向左浏览${label}`, next: `向右浏览${label}` };
  return cards => root.render(
    <DirectionProvider locale="zh-CN">
      <Carousel aria-label={label} trackId={trackId} arrowLabels={arrowLabels} edgeArrows showDots={false}
        className="home-library-carousel min-w-0">
        {cards.map(card => (
          <CarouselItem key={card.id} className="home-library-carousel-item">
            <LibraryCard card={card} />
          </CarouselItem>
        ))}
      </Carousel>
    </DirectionProvider>,
  );
}

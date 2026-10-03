//! Block-cell rendition sampled from the official logo, with no runtime image dependency.
use ratatui::{
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
    Frame,
};

pub const ORANGE: Color = Color::Rgb(255, 112, 32);
const COMPACT: &str = include_str!("brand-compact.txt");
const LARGE: &str = include_str!("brand-large.txt");

/// Preserve room for selection, chat and input; grow the mark only with the window.
pub fn height(area: Rect) -> u16 {
    if area.width >= 104 && area.height >= 44 {
        17
    } else if area.width >= 88 && area.height >= 34 {
        13
    } else {
        1
    }
}

fn color(pixel: u8) -> Option<Color> {
    match pixel {
        b'G' => Some(Color::Rgb(222, 228, 234)),
        b'O' => Some(ORANGE),
        _ => None,
    }
}

fn cell(top: u8, bottom: u8) -> Span<'static> {
    match (color(top), color(bottom)) {
        (None, None) => Span::raw(" "),
        (Some(c), None) => Span::styled("▀", Style::default().fg(c)),
        (None, Some(c)) => Span::styled("▄", Style::default().fg(c)),
        (Some(a), Some(b)) => Span::styled("▀", Style::default().fg(a).bg(b)),
    }
}

pub fn render(frame: &mut Frame<'_>, area: Rect) {
    let accent = Style::default().fg(ORANGE).add_modifier(Modifier::BOLD);
    let full = if area.height >= 17 && area.width >= 104 {
        Some(LARGE)
    } else if area.height >= 13 && area.width >= 88 {
        Some(COMPACT)
    } else {
        None
    };
    let lines = match full {
        Some(mask) => {
            let rows: Vec<_> = mask.lines().map(str::as_bytes).collect();
            rows.chunks_exact(2)
                .enumerate()
                .map(|(i, pair)| {
                    let mut spans = vec![Span::raw("  ")];
                    spans.extend(pair[0].iter().zip(pair[1]).map(|(&a, &b)| cell(a, b)));
                    spans.push(Span::raw("    "));
                    let middle = rows.len() / 4;
                    spans.push(match i {
                        n if n == middle.saturating_sub(2) => Span::styled("RoboArchon", accent),
                        n if n == middle => Span::raw("PHYSICAL AGENT"),
                        n if n == middle + 1 => Span::raw("FOR A MORE REAL WORLD"),
                        _ => Span::raw(""),
                    });
                    Line::from(spans)
                })
                .collect::<Vec<_>>()
        }
        None => vec![Line::from(Span::styled(" ▲  RoboArchon", accent))],
    };
    frame.render_widget(Paragraph::new(lines), area);
}

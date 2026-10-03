//! Braille-dot rendition of the official icon AND wordmark, sampled at build time.
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
const SLOGAN: &str = "PHYSICAL AGENT FOR A MORE REAL WORLD";

/// Leave at least fifteen rows for status, choices/chat and input.
pub fn height(area: Rect) -> u16 {
    if area.width >= 88 && area.height >= 44 {
        19
    } else if area.width >= 64 && area.height >= 34 {
        15
    } else {
        1
    }
}

fn cell(rows: &[&[u8]], x: usize) -> Span<'static> {
    // Braille: left dots 1,2,3,7 and right dots 4,5,6,8.
    let bits = [[0, 3], [1, 4], [2, 5], [6, 7]];
    let mut value = 0u32;
    let mut orange = 0;
    let mut gray = 0;
    for y in 0..4 {
        for dx in 0..2 {
            match rows[y][x + dx] {
                b'O' => {
                    orange += 1;
                    value |= 1 << bits[y][dx];
                }
                b'G' => {
                    gray += 1;
                    value |= 1 << bits[y][dx];
                }
                _ => {}
            }
        }
    }
    if value == 0 {
        Span::raw(" ")
    } else {
        let color = if orange >= gray {
            ORANGE
        } else {
            Color::Rgb(222, 228, 234)
        };
        Span::styled(
            char::from_u32(0x2800 + value).unwrap().to_string(),
            Style::default().fg(color),
        )
    }
}

pub fn render(frame: &mut Frame<'_>, area: Rect) {
    let full = if area.height >= 19 && area.width >= 88 {
        Some(LARGE)
    } else if area.height >= 15 && area.width >= 64 {
        Some(COMPACT)
    } else {
        None
    };
    let lines = match full {
        Some(mask) => {
            let rows: Vec<_> = mask.lines().map(str::as_bytes).collect();
            let width = rows[0].len() / 2;
            let inset = (area.width as usize).saturating_sub(width) / 2;
            let mut lines: Vec<_> = rows
                .chunks_exact(4)
                .map(|group| {
                    let mut spans = vec![Span::raw(" ".repeat(inset))];
                    spans.extend((0..rows[0].len()).step_by(2).map(|x| cell(group, x)));
                    Line::from(spans)
                })
                .collect();
            lines.push(Line::raw(""));
            lines.push(Line::from(vec![
                Span::raw(" ".repeat(inset + (width - SLOGAN.len()) / 2)),
                Span::styled(SLOGAN, Style::default().fg(Color::Gray)),
            ]));
            lines
        }
        None => vec![Line::from(Span::styled(
            " ▲  RoboArchon",
            Style::default().fg(ORANGE).add_modifier(Modifier::BOLD),
        ))],
    };
    frame.render_widget(Paragraph::new(lines), area);
}

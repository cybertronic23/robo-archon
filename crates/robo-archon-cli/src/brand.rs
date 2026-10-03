//! ASCII interpretation of the official dual-arm arch and triangular core.
use ratatui::{
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
    Frame,
};

pub const ORANGE: Color = Color::Rgb(255, 112, 32);
const ARMS: [&str; 7] = [
    "         ____        ____",
    "        /   /        \\   \\",
    "       /   /    /\\    \\   \\",
    "      (o)==\\   /__\\   /==(o)",
    "      / /  \\_      _/  \\ \\",
    "     (O)                (O)",
    "    _/ \\_              _/ \\_",
];

/// Keep room for selection, chat and input on small terminals.
pub fn height(area: Rect) -> u16 {
    if area.width >= 72 && area.height >= 30 {
        7
    } else {
        1
    }
}

pub fn render(frame: &mut Frame<'_>, area: Rect) {
    let ink = Style::default().fg(Color::Gray);
    let accent = Style::default().fg(ORANGE).add_modifier(Modifier::BOLD);
    let lines: Vec<Line> = if area.height >= 7 && area.width >= 72 {
        ARMS.iter()
            .enumerate()
            .map(|(i, arm)| {
                let mut spans = Vec::new();
                // Orange bearings and the central triangle mirror the source artwork.
                for (column, c) in arm.chars().enumerate() {
                    spans.push(Span::styled(
                        c.to_string(),
                        if c == 'O'
                            || ((i == 2 || i == 3) && (14..=17).contains(&column) && c != ' ')
                        {
                            accent
                        } else {
                            ink
                        },
                    ));
                }
                spans.push(Span::raw(" ".repeat(30usize.saturating_sub(arm.len()))));
                spans.push(match i {
                    2 => Span::styled("    RoboArchon", accent),
                    4 => Span::styled("    PHYSICAL AGENT", ink),
                    5 => Span::styled("    FOR A MORE REAL WORLD", ink),
                    _ => Span::raw(""),
                });
                Line::from(spans)
            })
            .collect()
    } else {
        vec![Line::from(vec![Span::styled(" /\\  RoboArchon", accent)])]
    };
    frame.render_widget(Paragraph::new(lines), area);
}

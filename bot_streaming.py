import json
import os
from dotenv import load_dotenv
import discord
import datetime
import asyncio
import io
import re
from keep_live import keep_alive

from discord.ext import commands, tasks
from discord import app_commands, Embed, PermissionOverwrite
import discord.ui as ui

# ---------- Constantes ----------
DATA_FILES = {
    "films": "data/films.json",
    "series": "data/series.json",
    "jeux": "data/jeux.json",
    "logiciels": "data/logiciels.json",
}
LOG_CHANNEL_NAME = "logs"

VOICE_CHANNEL_MAP = {
    "films": "Films disponibles",
    "series": "Series disponibles",
    "jeux": "Jeux disponibles",
    "logiciels": "Logiciels disponibles",
}

os.makedirs("data", exist_ok=True)

# ---------- Fonctions Utilitaire de Données ----------
def load_data(category: str) -> dict:
    filepath = DATA_FILES.get(category)
    if not filepath:
        raise ValueError(f"Category '{category}' not configured in DATA_FILES.")

    if not os.path.isfile(filepath):
        print(f"INFO: Creating empty JSON file for {category} at {filepath}")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
        return {}

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            if not content.strip():
                print(f"INFO: JSON file for {category} at {filepath} is empty. Returning empty dict.")
                return {}
            data = json.loads(content)
            
            # --- LOGIQUE DE MIGRATION SIMPLE POUR LES SERIES ---
            if category == "series":
                for title, item_data in data.items():
                    if "url" in item_data and "seasons" not in item_data:
                        # Convertit l'ancienne structure en nouvelle avec une saison 1
                        print(f"INFO: Migrating old series format for '{title}' to new season-based format.")
                        item_data["seasons"] = [{"number": 1, "title": "Saison 1", "url": item_data["url"]}]
                        del item_data["url"] # Supprime l'ancienne clé 'url'
            # --- FIN LOGIQUE DE MIGRATION ---
            return data
    except json.JSONDecodeError:
        print(f"⚠️ Warning: Corrupted JSON file detected for {category} at {filepath}. Resetting to empty JSON.")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
        return {}
    except Exception as e:
        print(f"❌ Error loading data for {category} from {filepath}: {e}")
        return {}

def save_data(category: str, data: dict):
    filepath = DATA_FILES.get(category)
    if not filepath:
        raise ValueError(f"Category '{category}' not configured in DATA_FILES.")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ---------- Configuration du Bot ----------
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Autocomplétion pour les Commandes Slash ----------
async def autocomplete_item_title(interaction: discord.Interaction, current: str):
    command_name = interaction.command.name
    category_map = {
        "addfilm": "films", "delfilm": "films", "getfilm": "films",
        "addserieseason": "series", "delserieseason": "series", "getserie": "series", "delseries": "series", "importseries": "series",
        "addjeu": "jeux", "deljeu": "jeux", "getjeu": "jeux",
        "addlogiciel": "logiciels", "dellogiciel": "logiciels", "getlogiciel": "logiciels",
    }
    category = category_map.get(command_name)
    if not category:
        return []

    current_data = load_data(category) 
    return [app_commands.Choice(name=nom.title(), value=nom)
            for nom in current_data if current.lower() in nom.lower()][:5]

# ---------- Fonctions d'Aide ----------
def stars_from_rating(rating: float) -> str:
    full = int(round(rating))
    return "★" * full + "☆" * (5 - full)

def get_all_genres(category: str) -> list[str]:
    current_data = load_data(category)
    genres = set()
    for item_data in current_data.values():
        if 'themes' in item_data:
            genres.update(item_data['themes'])
    return sorted(list(genres))

def get_items_by_genre(category: str, genre: str) -> dict:
    current_data = load_data(category)
    return {
        title: data for title, data in current_data.items()
        if genre in data.get('themes', [])
    }

def get_all_items_sorted(category: str) -> dict:
    current_data = load_data(category)
    return dict(sorted(current_data.items()))

# ---------- Système de Pagination ----------
class PaginatedView(ui.View):
    def __init__(self, items: dict, items_per_page: int, embed_title: str, embed_color: int, item_category_singular: str, data_file_category: str):
        super().__init__(timeout=180)
        self.items = items
        self.item_titles = sorted(items.keys())
        self.items_per_page = items_per_page
        self.embed_title = embed_title
        self.embed_color = embed_color
        self.item_category_singular = item_category_singular # Pour l'affichage (film, série, jeu)
        self.data_file_category = data_file_category       # Pour charger les données (films, series, jeux)
        self.current_page = 0
        self.message: discord.Message = None 
        self.update_buttons()

    def get_page_content(self) -> str:
        start_index = self.current_page * self.items_per_page
        end_index = start_index + self.items_per_page

        page_items = self.item_titles[start_index:end_index]
        if not page_items:
            return "Aucun élément trouvé pour cette page."

        description = []
        for title in page_items:
            description.append(f"• **{title.title()}**")
        return "\n".join(description)

    def create_page_embed(self) -> Embed:
        total_pages = (len(self.item_titles) + self.items_per_page - 1) // self.items_per_page
        embed = Embed(
            title=self.embed_title,
            description=self.get_page_content(),
            color=self.embed_color
        )
        embed.set_footer(text=f"Page {self.current_page + 1}/{total_pages} | {self.item_category_singular.capitalize()}s par page: {self.items_per_page}\nCliquez sur un {self.item_category_singular} pour voir ses détails.")
        
        self.clear_items() 
        
        if total_pages > 1:
            prev_button = ui.Button(label="◀️ Précédent", style=discord.ButtonStyle.secondary, custom_id="prev_page", disabled=self.current_page == 0)
            next_button = ui.Button(label="Suivant ▶️", style=discord.ButtonStyle.secondary, custom_id="next_page", disabled=self.current_page >= total_pages - 1)
            self.add_item(prev_button)
            self.add_item(next_button)

        start_index = self.current_page * self.items_per_page
        end_index = start_index + self.items_per_page
        page_items = self.item_titles[start_index:end_index]

        for i, title in enumerate(page_items):
            button = ui.Button(
                label=title.title(),
                style=discord.ButtonStyle.primary,
                custom_id=f"view_item_{self.data_file_category}_{title.lower()}" # Utilise data_file_category ici
            )
            self.add_item(button)
        
        return embed

    def update_buttons(self):
        pass

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, ui.Button):
                item.disabled = True
        if self.message: 
            try:
                await self.message.edit(view=self)
            except Exception as e:
                print(f"Error on_timeout editing message: {e}")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type != discord.InteractionType.component:
            return False 

        custom_id = interaction.data.get('custom_id')

        if custom_id == "prev_page" or custom_id == "next_page":
            total_pages = (len(self.item_titles) + self.items_per_page - 1) // self.items_per_page

            if custom_id == "prev_page":
                self.current_page = max(0, self.current_page - 1)
            elif custom_id == "next_page":
                self.current_page = min(total_pages - 1, self.current_page + 1)
            
            await interaction.response.edit_message(embed=self.create_page_embed(), view=self)
            return False 

        elif custom_id.startswith(f"view_item_{self.data_file_category}_"): # Utilise data_file_category ici
            item_title_lower = custom_id.replace(f"view_item_{self.data_file_category}_", "")
            
            current_data = load_data(self.data_file_category) # Utilise la bonne catégorie pour charger
            
            if item_title_lower in current_data:
                await interaction.response.send_message(
                    embed=make_item_embed(self.data_file_category, item_title_lower, current_data[item_title_lower]),
                    view=ItemDetailsView(self.data_file_category, item_title_lower),
                    ephemeral=True 
                )
            else:
                await interaction.response.send_message(f"❌ Détails de l'{self.item_category_singular} introuvables.", ephemeral=True)
            return False 
        
        return True 

# ---------- Modale de Notation et Vue de Détails d'Élément ----------
class RatingModal(ui.Modal, title="Noter l'élément"):
    def __init__(self, category: str, item_title: str):
        super().__init__()
        self.category = category
        self.item_title = item_title
        self.rating_input = ui.TextInput(
            label="Note (entre 1 et 5)",
            placeholder="Saisis une note de 1 à 5",
            required=True,
            max_length=1,
            min_length=1
        )
        self.add_item(self.rating_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            note = int(self.rating_input.value)
            if not 1 <= note <= 5:
                return await interaction.response.send_message("❌ La note doit être un nombre entre 1 et 5.", ephemeral=True)

            current_data = load_data(self.category)
            key = self.item_title.lower()

            if key not in current_data:
                return await interaction.response.send_message(f"❌ {self.category.capitalize()} introuvable.", ephemeral=True)

            current_data[key].setdefault('ratings', []).append(note)
            avg = sum(current_data[key]['ratings']) / len(current_data[key]['ratings'])
            current_data[key]['rating'] = round(avg, 2)
            save_data(self.category, current_data)

            updated_embed = make_item_embed(self.category, key, current_data[key])
            updated_view = ItemDetailsView(self.category, key)

            if interaction.message and interaction.message.flags.ephemeral:
                await interaction.response.edit_message(embed=updated_embed, view=updated_view)
            else:
                await interaction.response.send_message(
                    f"✅ **{self.item_title.title()}** noté ! Note actuelle : {stars_from_rating(current_data[key]['rating'])}",
                    embed=updated_embed, 
                    ephemeral=True
                )

        except ValueError:
            await interaction.response.send_message("❌ La note doit être un nombre entier.", ephemeral=True)
        except Exception as e:
            print(f"Error submitting rating: {e}")
            await interaction.response.send_message("❌ Une erreur s'est produite lors de la soumission de la note.", ephemeral=True)

class ItemDetailsView(ui.View):
    def __init__(self, category: str, item_title: str):
        super().__init__(timeout=180)
        self.category = category
        self.item_title = item_title

    @ui.button(label="⭐ Noter", style=discord.ButtonStyle.green, custom_id="rate_item_button")
    async def rate_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(RatingModal(self.category, self.item_title))

# ---------- Embeds ----------
def make_item_embed(category: str, title: str, meta: dict) -> discord.Embed:
    rating = meta.get('rating')
    note_display = stars_from_rating(rating) if rating is not None else 'Pas encore noté'

    embed_description = ""
    if category == "series":
        embed_description += f"Note: {note_display}\n\n"
        seasons = sorted(meta.get('seasons', []), key=lambda s: s.get('number', 0))
        if seasons:
            embed_description += "**Saisons disponibles :**\n"
            for season in seasons:
                s_num = season.get('number', '??')
                s_title = season.get('title', f"Saison {s_num}")
                s_url = season.get('url', '#')
                embed_description += f"- [{s_title}]({s_url})\n"
        else:
            embed_description += "Pas de saison trouvée pour cette série."
    else: 
        # Déterminer le singulier pour l'affichage
        display_category_singular = category[:-1] if category.endswith('s') else category
        url_display = f"[🔗 Voir l'{display_category_singular}]({meta['url']})" if 'url' in meta else "Lien non disponible"
        embed_description = f"{url_display}\n\nNote: {note_display}"

    embed = Embed(
        title=title.title(),
        description=embed_description,
        color=0x1abc9c
    )
    if meta.get("image"):
        embed.set_image(url=meta["image"])
    if meta.get("themes"):
        embed.add_field(name="Genres/Thèmes", value=", ".join([theme.title() for theme in meta['themes']]), inline=False)
    return embed

def create_search_embed(category: str) -> discord.Embed:
    if category in ["jeux", "logiciels"]:
        title_text = "Nom"
    else:
        title_text = "Titre"

    # Déterminer le singulier pour l'affichage dans le titre de l'embed
    display_category_singular = category[:-1] if category.endswith('s') else category
    
    return Embed(
        title=f"🔍 Rechercher un {display_category_singular.title()}",
        description=f"Utilise le menu déroulant pour les genres/thèmes ou saisis un {title_text} pour rechercher.",
        color=0xffffff
    )

def create_ticket_embed(user: discord.User) -> discord.Embed:
    embed = Embed(
        title="🎫 Ticket Ouvert",
        description=(
            f"Ticket créé par {user.mention}.\n\n"
            "Explique ton problème ici."
        ),
        color=0xf5f5f5,
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_footer(text=f"Ticket de {user.display_name}", icon_url=user.display_avatar.url)
    return embed

# ---------- Modales et Vues pour la Recherche ----------
class ItemSearchModal(ui.Modal):
    def __init__(self, category: str):
        super().__init__(title=f"🔍 Recherche de {category[:-1] if category.endswith('s') else category}")
        self.category = category

        if category in ["jeux", "logiciels"]:
            label_text = f"Nom de l'{category[:-1] if category.endswith('s') else category}"
            placeholder_text = f"Nom de l'{category[:-1] if category.endswith('s') else category}..."
        else:
            label_text = f"Titre de l'{category[:-1] if category.endswith('s') else category}"
            placeholder_text = f"Titre de l'{category[:-1] if category.endswith('s') else category}..."

        self.item_name = ui.TextInput(label=label_text, placeholder=placeholder_text, required=False, max_length=100)
        self.add_item(self.item_name)

    async def on_submit(self, interaction: discord.Interaction):
        title = self.item_name.value.strip().lower()
        current_data = load_data(self.category)
        if title and title in current_data:
            return await interaction.response.send_message(
                embed=make_item_embed(self.category, title, current_data[title]),
                view=ItemDetailsView(self.category, title),
                ephemeral=True
            )
        await interaction.response.send_message(f"❌ Aucun {self.category[:-1] if self.category.endswith('s') else self.category} trouvé avec ce titre/nom.", ephemeral=True)

class ItemGenreSelect(ui.Select):
    def __init__(self, category: str):
        self.category = category
        all_genres = get_all_genres(category)
        options = [
            discord.SelectOption(label=genre.title(), value=genre)
            for genre in all_genres
        ]

        if not options:
            options.append(discord.SelectOption(label="Aucun genre disponible", value="no_genres_available", default=True))
            super().__init__(placeholder="Aucun genre disponible...", min_values=1, max_values=1, options=options, custom_id=f"{category}_genre_select", disabled=True)
        else:
            if len(options) > 25:
                options = options[:24] + [discord.SelectOption(label="Plus de genres...", value="more_genres_option")]
            super().__init__(placeholder="Sélectionne un genre...", min_values=1, max_values=1, options=options, custom_id=f"{category}_genre_select")

    async def callback(self, interaction: discord.Interaction):
        selected_genre = self.values[0]

        if selected_genre == "no_genres_available":
            await interaction.response.send_message("Il n'y a actuellement aucun genre disponible à rechercher. Veuillez ajouter des éléments avec des genres.", ephemeral=True)
            return
        elif selected_genre == "more_genres_option":
            await interaction.response.send_message("Nous avons plus de genres! Veuillez affiner votre recherche par titre/nom ou contacter un administrateur.", ephemeral=True)
            return

        items_by_genre = get_items_by_genre(self.category, selected_genre)

        if items_by_genre:
            # déterminer le singulier pour l'affichage
            display_category_singular = self.category[:-1] if self.category.endswith('s') else self.category
            paginated_view = PaginatedView(
                items_by_genre, 10, 
                f"{self.category.title()} - Genre: {selected_genre.title()}",
                0xffcc00,
                display_category_singular, # item_category_singular (pour affichage)
                self.category               # data_file_category (pour load_data)
            )
            await interaction.response.send_message(embed=paginated_view.create_page_embed(), ephemeral=True)
            paginated_view.message = await interaction.original_response() 
            await paginated_view.message.edit(view=paginated_view) 
        else:
            await interaction.response.send_message(f"❌ Aucun {self.category[:-1] if self.category.endswith('s') else self.category} trouvé pour ce genre.", ephemeral=True)

class SearchCategoryView(ui.View):
    def __init__(self, category: str):
        super().__init__(timeout=None)
        self.category = category # Ceci est la catégorie au pluriel comme dans DATA_FILES
        
        # Déterminer le singulier pour l'affichage des boutons
        self.display_category_singular = category[:-1] if category.endswith('s') else category

        self.add_item(ItemGenreSelect(category))

        search_label = "🔍 Rechercher par Titre"
        if category in ["jeux", "logiciels"]:
            search_label = "🔍 Rechercher par Nom"
        
        self._search_button = ui.Button(label=search_label, style=discord.ButtonStyle.primary, custom_id=f"{category}_search_by_title")
        self.add_item(self._search_button)

        self._view_all_button = ui.Button(label="📖 Voir tout", style=discord.ButtonStyle.secondary, custom_id=f"{category}_view_all_items")
        self.add_item(self._view_all_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.data and 'custom_id' in interaction.data:
            custom_id = interaction.data['custom_id']
            if custom_id == f"{self.category}_search_by_title":
                await interaction.response.send_modal(ItemSearchModal(self.category))
                return False
            elif custom_id == f"{self.category}_view_all_items":
                all_items = get_all_items_sorted(self.category)
                if not all_items:
                    await interaction.response.send_message(f"❌ Aucun {self.display_category_singular} disponible pour l'instant.", ephemeral=True)
                    return False

                paginated_view = PaginatedView(
                    all_items, 10,
                    f"Tous les {self.category.title()}",
                    0x00bfff,
                    self.display_category_singular, # item_category_singular (pour affichage)
                    self.category                   # data_file_category (pour load_data)
                )
                await interaction.response.send_message(embed=paginated_view.create_page_embed(), ephemeral=True)
                paginated_view.message = await interaction.original_response() 
                await paginated_view.message.edit(view=paginated_view) 
                return False
        return True

class FilmSearchView(SearchCategoryView):
    def __init__(self):
        super().__init__("films")

class SerieSearchView(SearchCategoryView):
    def __init__(self):
        super().__init__("series")

class JeuSearchView(SearchCategoryView):
    def __init__(self):
        super().__init__("jeux")

class LogicielSearchView(SearchCategoryView):
    def __init__(self):
        super().__init__("logiciels")

# ---------- Vues pour les Tickets ----------
class TicketCloseView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="📝 Archiver le ticket", style=discord.ButtonStyle.secondary, custom_id="ticket_archive")
    async def archive_ticket(self, interaction: discord.Interaction, button: ui.Button):
        ticket_channel = interaction.channel
        logs_channel = discord.utils.get(interaction.guild.text_channels, name=LOG_CHANNEL_NAME)

        if not logs_channel:
            return await interaction.response.send_message(f"❌ Le salon de logs (`#{LOG_CHANNEL_NAME}`) est introuvable. Veuillez le créer.", ephemeral=True)

        await interaction.response.send_message("Archivage du ticket en cours...", ephemeral=True)

        messages = []
        async for msg in ticket_channel.history(limit=None, oldest_first=True):
            ts = msg.created_at.replace(tzinfo=datetime.timezone.utc).astimezone(tz=None).strftime('%Y-%m-%d %H:%M:%S')
            content = msg.content if msg.content else "[Contenu non textuel]"
            messages.append(f"[{ts}] {msg.author.display_name} ({msg.author.id}): {content}")

        log_content = "\n".join(messages)
        file_name = f"{ticket_channel.name}_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

        if len(log_content) > 1900:
            log_file = discord.File(
                fp=io.BytesIO(log_content.encode('utf-8')),
                filename=file_name
            )
            await logs_channel.send(f"Journal du ticket **{ticket_channel.name}** archivé par {interaction.user.mention}:", file=log_file)
        else:
            await logs_channel.send(f"Journal du ticket **{ticket_channel.name}** archivé par {interaction.user.mention}:\n```\n{log_content}\n```")

        await interaction.followup.send(f"✅ Ticket archivé dans {logs_channel.mention}.", ephemeral=True)

    @ui.button(label="❌ Fermer le ticket", style=discord.ButtonStyle.red, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("📦 Ticket fermé. Suppression dans 5s...", ephemeral=True)
        await asyncio.sleep(5)
        await interaction.channel.delete()

class TicketView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🎟️ Ouvrir un ticket", style=discord.ButtonStyle.green, custom_id="open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: ui.Button):
        guild = interaction.guild
        author = interaction.user

        if any(ch.name == f"ticket-{author.name.lower()}" for ch in guild.text_channels):
            return await interaction.response.send_message("❗ Tu as déjà un ticket ouvert.", ephemeral=True)

        overwrites = {
            guild.default_role: PermissionOverwrite(read_messages=False),
            author: PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: PermissionOverwrite(read_messages=True, send_messages=True)
        }

        ticket = await guild.create_text_channel(f"ticket-{author.name.lower()}", overwrites=overwrites)
        await ticket.send(embed=create_ticket_embed(author), view=TicketCloseView())
        await interaction.response.send_message(f"✅ Ticket créé: {ticket.mention}", ephemeral=True)

# --- Fonction d'aide pour envoyer/nettoyer les embeds de recherche/ticket ---
async def send_and_cleanup_embed(channel: discord.TextChannel, embed: discord.Embed, view: ui.View, bot_user: discord.ClientUser):
    try:
        bot_messages = []
        async for msg in channel.history(limit=100):
            if msg.author == bot_user and msg.embeds: 
                if msg.embeds[0].title and (
                    msg.embeds[0].title.startswith("🔍 Rechercher un") or
                    msg.embeds[0].title.startswith("Besoin d'aide ?") 
                ):
                    bot_messages.append(msg)

        for msg in bot_messages:
            try:
                await msg.delete()
                await asyncio.sleep(0.5) 
            except discord.NotFound:
                pass 
            except Exception as e:
                print(f"Error deleting old bot message in channel '{channel.name}': {e}")
        
        sent_message = await channel.send(embed=embed, view=view)
        
        if view.timeout is None: 
            bot.add_view(view) 
        return sent_message 

    except discord.Forbidden:
        print(f"❌ Missing permissions to purge messages or send embeds in channel '{channel.name}' (ID: {channel.id}).")
    except Exception as e:
        print(f"❌ An error occurred while sending/cleaning embed in channel '{channel.name}': {e}")
    return None

# ---------- Commandes Slash d'Initialisation des Embeds (Admin) ----------
@bot.tree.command(description="Envoyer l'embed de recherche de film dans #recherche_films (Admin)")
@app_commands.default_permissions(manage_guild=True)
async def setup_recherche_films_embed(interaction: discord.Interaction):
    salon = discord.utils.get(interaction.guild.text_channels, name="recherche_films")
    if not salon:
        return await interaction.response.send_message("❌ Le salon `#recherche_films` est introuvable.", ephemeral=True)
    embed = create_search_embed("films")
    view = FilmSearchView()
    await interaction.response.defer(ephemeral=True) 
    await send_and_cleanup_embed(salon, embed, view, interaction.client.user)
    await interaction.followup.send("✅ Embed de recherche de films mis à jour.", ephemeral=True)

@bot.tree.command(description="Envoyer l'embed de recherche de séries dans #recherche_series (Admin)")
@app_commands.default_permissions(manage_guild=True)
async def setup_recherche_series_embed(interaction: discord.Interaction):
    salon = discord.utils.get(interaction.guild.text_channels, name="recherche_series")
    if not salon:
        return await interaction.response.send_message("❌ Le salon `#recherche_series` est introuvable.", ephemeral=True)
    embed = create_search_embed("series")
    view = SerieSearchView()
    await interaction.response.defer(ephemeral=True)
    await send_and_cleanup_embed(salon, embed, view, interaction.client.user)
    await interaction.followup.send("✅ Embed de recherche de séries mis à jour.", ephemeral=True)

@bot.tree.command(description="Envoyer l'embed de recherche de jeux dans #recherche_jeux (Admin)")
@app_commands.default_permissions(manage_guild=True)
async def setup_recherche_jeux_embed(interaction: discord.Interaction):
    salon = discord.utils.get(interaction.guild.text_channels, name="recherche_jeux")
    if not salon:
        return await interaction.response.send_message("❌ Le salon `#recherche_jeux` est introuvable.", ephemeral=True)
    embed = create_search_embed("jeux")
    view = JeuSearchView()
    await interaction.response.defer(ephemeral=True)
    await send_and_cleanup_embed(salon, embed, view, interaction.client.user)
    await interaction.followup.send("✅ Embed de recherche de jeux mis à jour.", ephemeral=True)

@bot.tree.command(description="Envoyer l'embed de recherche de logiciels dans #recherche_logiciels (Admin)")
@app_commands.default_permissions(manage_guild=True)
async def setup_recherche_logiciels_embed(interaction: discord.Interaction):
    salon = discord.utils.get(interaction.guild.text_channels, name="recherche_logiciels")
    if not salon:
        return await interaction.response.send_message("❌ Le salon `#recherche_logiciels` est introuvable.", ephemeral=True)
    embed = create_search_embed("logiciels")
    view = LogicielSearchView()
    await interaction.response.defer(ephemeral=True)
    await send_and_cleanup_embed(salon, embed, view, interaction.client.user)
    await interaction.followup.send("✅ Embed de recherche de logiciels mis à jour.", ephemeral=True)

@bot.tree.command(description="Envoyer l'embed pour ouvrir un ticket dans #demande (Admin)")
@app_commands.default_permissions(manage_guild=True)
async def setup_ticket_embed(interaction: discord.Interaction):
    demande_channel = discord.utils.get(interaction.guild.text_channels, name="demande")
    if not demande_channel:
        return await interaction.response.send_message("❌ Le salon `#demande` est introuvable.", ephemeral=True)

    embed = discord.Embed(
        title="Besoin d'aide ?",
        description="Clique sur le bouton ci-dessous pour ouvrir un ticket et obtenir de l'aide.",
        color=0x00ff00
    )
    view = TicketView()
    await interaction.response.defer(ephemeral=True)
    await send_and_cleanup_embed(demande_channel, embed, view, bot.user)
    await interaction.followup.send("✅ Embed de création de ticket mis à jour dans #demande.", ephemeral=True)

@bot.tree.command(name="clear", description="Supprimer un nombre de messages dans le salon actuel (Admin)")
@app_commands.describe(nombre="Nombre de messages à supprimer")
@app_commands.default_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, nombre: int):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=nombre)
    await interaction.followup.send(f"🧹 **{len(deleted)}** messages supprimés.", ephemeral=True)

@bot.tree.command(description="Poster un message dans le salon #demande (Staff)")
@app_commands.describe(message="Le message à poster dans le salon #demande")
@app_commands.default_permissions(manage_guild=True)
async def postdemande(interaction: discord.Interaction, message: str):
    chan = discord.utils.get(interaction.guild.text_channels, name="demande")
    if chan is None:
        return await interaction.response.send_message("❌ Le salon `#demande` est introuvable.", ephemeral=True)

    await chan.send(f"📢 Message de {interaction.user.mention} : {message}")
    await interaction.response.send_message("✅ Message posté dans #demande.", ephemeral=True)

# ---------- Commandes Slash Générales (Ajouter/Supprimer/Obtenir) ----------
async def add_item_command(interaction: discord.Interaction, category: str, titre: str, url: str, image: str | None = None, themes: str | None = None):
    """Fonction générique pour ajouter un élément à une catégorie (hors séries)."""
    await interaction.response.defer(ephemeral=False) 

    key = titre.lower()
    data = {"url": url, "image": image}
    if themes:
        data['themes'] = [g.strip().lower() for g in themes.split(',')]

    current_data = load_data(category)
    current_data[key] = data
    save_data(category, current_data)

    response_embed = Embed(
        title=f"✅ {titre.title()} ajouté à la catégorie {category.title()} !",
        description=f"Lien : [Cliquez ici]({url})",
        color=0x1abc9c
    )
    if image:
        response_embed.set_image(url=image)
    if themes:
        response_embed.add_field(name="Genres/Thèmes", value=", ".join([g.title() for g in data['themes']]), inline=False)

    await interaction.followup.send(
        embed=response_embed,
        view=ItemDetailsView(category, key) 
    )
    await update_search_channel_embed(interaction.guild, category)
    await update_voice_channel_names_for_guild(interaction.guild)


async def del_item_command(interaction: discord.Interaction, category: str, titre: str):
    """Fonction générique pour supprimer un élément d'une catégorie (hors séries)."""
    key = titre.lower()
    current_data = load_data(category)
    if key in current_data:
        current_data.pop(key)
        save_data(category, current_data)
        await interaction.response.send_message(f"🗑️ {category.title()} **{titre.title()}** supprimé.")
        await update_search_channel_embed(interaction.guild, category)
        await update_voice_channel_names_for_guild(interaction.guild)
    else:
        await interaction.response.send_message(f"❌ {category.title()} introuvable.", ephemeral=True)

async def get_item_command(interaction: discord.Interaction, category: str, titre: str):
    """Fonction générique pour obtenir et afficher les détails d'un élément."""
    key = titre.lower()
    current_data = load_data(category)
    if key in current_data:
        await interaction.response.send_message(
            embed=make_item_embed(category, key, current_data[key]),
            view=ItemDetailsView(category, key),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(f"❌ {category.title()} introuvable.", ephemeral=True)

# ---------- Commandes Slash Spécifiques (Ajouter/Supprimer/Obtenir par Catégorie) ----------
# Films, Jeux, Logiciels
@bot.tree.command(description="Ajouter un film à la base de données (Admin)")
@app_commands.describe(titre="Titre complet", url="Lien vers le film", image="Lien de l'image", themes="Genres (séparés par des virgules)")
@app_commands.default_permissions(manage_guild=True)
async def addfilm(interaction: discord.Interaction, titre: str, url: str, image: str | None = None, themes: str | None = None):
    await add_item_command(interaction, "films", titre, url, image, themes)

@bot.tree.command(description="Supprimer un film (Admin)")
@app_commands.describe(titre="Titre du film à supprimer")
@app_commands.autocomplete(titre=autocomplete_item_title)
@app_commands.default_permissions(manage_guild=True)
async def delfilm(interaction: discord.Interaction, titre: str):
    await del_item_command(interaction, "films", titre)

@bot.tree.command(description="Afficher les détails d'un film")
@app_commands.describe(titre="Titre du film")
@app_commands.autocomplete(titre=autocomplete_item_title)
async def getfilm(interaction: discord.Interaction, titre: str):
    await get_item_command(interaction, "films", titre)

@bot.tree.command(description="Ajouter un jeu (Admin)")
@app_commands.describe(titre="Nom complet", url="Lien vers le jeu", image="Lien de l'image", themes="Genres (séparés par des virgules)")
@app_commands.default_permissions(manage_guild=True)
async def addjeu(interaction: discord.Interaction, titre: str, url: str, image: str | None = None, themes: str | None = None):
    await add_item_command(interaction, "jeux", titre, url, image, themes)

@bot.tree.command(description="Supprimer un jeu (Admin)")
@app_commands.describe(titre="Nom du jeu à supprimer")
@app_commands.autocomplete(titre=autocomplete_item_title)
@app_commands.default_permissions(manage_guild=True)
async def deljeu(interaction: discord.Interaction, titre: str):
    await del_item_command(interaction, "jeux", titre)

@bot.tree.command(description="Afficher les détails d'un jeu")
@app_commands.describe(titre="Nom du jeu")
@app_commands.autocomplete(titre=autocomplete_item_title)
async def getjeu(interaction: discord.Interaction, titre: str):
    await get_item_command(interaction, "jeux", titre)

@bot.tree.command(description="Ajouter un logiciel (Admin)")
@app_commands.describe(titre="Nom complet", url="Lien vers le logiciel", image="Lien de l'image", themes="Catégories (séparés par des virgules)")
@app_commands.default_permissions(manage_guild=True)
async def addlogiciel(interaction: discord.Interaction, titre: str, url: str, image: str | None = None, themes: str | None = None):
    await add_item_command(interaction, "logiciels", titre, url, image, themes)

@bot.tree.command(description="Supprimer un logiciel (Admin)")
@app_commands.describe(titre="Nom du logiciel à supprimer")
@app_commands.autocomplete(titre=autocomplete_item_title)
@app_commands.default_permissions(manage_guild=True)
async def dellogiciel(interaction: discord.Interaction, titre: str):
    await del_item_command(interaction, "logiciels", titre)

@bot.tree.command(description="Afficher les détails d'un logiciel")
@app_commands.describe(titre="Nom du logiciel")
@app_commands.autocomplete(titre=autocomplete_item_title)
async def getlogiciel(interaction: discord.Interaction, titre: str):
    await get_item_command(interaction, "logiciels", titre)

# --- COMMANDES SPÉCIFIQUES AUX SÉRIES (NOUVELLES LOGIQUES) ---
@bot.tree.command(name="addserieseason", description="Ajouter ou mettre à jour une saison pour une série (Admin)")
@app_commands.describe(
    titre="Titre de la série",
    season_number="Numéro de la saison",
    season_url="Lien de la saison",
    season_title="Titre de la saison (ex: Arc du train de l'infini, laisser vide pour 'Saison X')",
    image="Lien de l'image de la série (utilisé seulement si nouvelle série)",
    themes="Genres de la série (séparés par des virgules, utilisé seulement si nouvelle série)"
)
@app_commands.default_permissions(manage_guild=True)
async def addserieseason(
    interaction: discord.Interaction,
    titre: str,
    season_number: int,
    season_url: str,
    season_title: str | None = None,
    image: str | None = None,
    themes: str | None = None
):
    await interaction.response.defer(ephemeral=False)

    key = titre.lower()
    current_data = load_data("series")

    if key not in current_data:
        new_series_data = {
            "image": image,
            "themes": [g.strip().lower() for g in themes.split(',')] if themes else [],
            "seasons": []
        }
        current_data[key] = new_series_data
        response_msg_start = f"✅ Nouvelle série **{titre.title()}** créée."
    else:
        response_msg_start = f"✅ Série **{titre.title()}** mise à jour."
        if image:
            current_data[key]["image"] = image
        if themes:
            current_data[key]["themes"] = [g.strip().lower() for g in themes.split(',')]
    
    season_exists = False
    for i, season in enumerate(current_data[key]["seasons"]):
        if season.get("number") == season_number:
            current_data[key]["seasons"][i]["url"] = season_url
            current_data[key]["seasons"][i]["title"] = season_title if season_title else f"Saison {season_number}"
            season_exists = True
            response_msg_start += f" La saison {season_number} a été mise à jour."
            break
    
    if not season_exists:
        new_season = {
            "number": season_number,
            "title": season_title if season_title else f"Saison {season_number}",
            "url": season_url
        }
        current_data[key]["seasons"].append(new_season)
        response_msg_start += f" La saison {season_number} a été ajoutée."
        current_data[key]["seasons"].sort(key=lambda s: s.get('number', 0))

    save_data("series", current_data)

    embed = Embed(
        title=response_msg_start,
        description=f"La série **{titre.title()}** a été mise à jour avec les informations suivantes :",
        color=0x1abc9c
    )
    if current_data[key].get("image"): 
        embed.set_image(url=current_data[key]["image"])
    if current_data[key].get("themes"):
        embed.add_field(name="Genres/Thèmes", value=", ".join([t.title() for t in current_data[key]['themes']]), inline=False)
    
    await interaction.followup.send(embed=embed)
    await update_search_channel_embed(interaction.guild, "series")
    await update_voice_channel_names_for_guild(interaction.guild)

@bot.tree.command(name="delserieseason", description="Supprimer une saison spécifique d'une série (Admin)")
@app_commands.describe(
    titre="Titre de la série",
    season_number="Numéro de la saison à supprimer"
)
@app_commands.autocomplete(titre=autocomplete_item_title)
@app_commands.default_permissions(manage_guild=True)
async def delserieseason(interaction: discord.Interaction, titre: str, season_number: int):
    key = titre.lower()
    current_data = load_data("series")

    if key not in current_data:
        return await interaction.response.send_message(f"❌ Série **{titre.title()}** introuvable.", ephemeral=True)

    seasons = current_data[key].get("seasons", [])
    
    initial_season_count = len(seasons)
    current_data[key]["seasons"] = [s for s in seasons if s.get("number") != season_number]
    
    if len(current_data[key]["seasons"]) == initial_season_count:
        return await interaction.response.send_message(f"❌ Saison {season_number} introuvable pour la série **{titre.title()}**.", ephemeral=True)
    
    if not current_data[key]["seasons"]:
        current_data.pop(key)
        response_msg = f"🗑️ Saison {season_number} de **{titre.title()}** supprimée. C'était la dernière saison, donc la série a été complètement supprimée."
    else:
        response_msg = f"🗑️ Saison {season_number} de **{titre.title()}** supprimée."
        
    save_data("series", current_data)
    await interaction.response.send_message(response_msg)
    await update_search_channel_embed(interaction.guild, "series")
    await update_voice_channel_names_for_guild(interaction.guild)

@bot.tree.command(name="delseries", description="Supprimer une série entière (Admin)")
@app_commands.describe(titre="Titre de la série à supprimer entièrement")
@app_commands.autocomplete(titre=autocomplete_item_title)
@app_commands.default_permissions(manage_guild=True)
async def delseries(interaction: discord.Interaction, titre: str):
    await del_item_command(interaction, "series", titre)

@bot.tree.command(description="Afficher les détails d'une série")
@app_commands.describe(titre="Titre de la série")
@app_commands.autocomplete(titre=autocomplete_item_title)
async def getserie(interaction: discord.Interaction, titre: str):
    await get_item_command(interaction, "series", titre)

@bot.tree.command(name="importseries", description="Ajoute une série avec plusieurs saisons en une seule fois (Admin)")
@app_commands.describe(
    titre="Titre de la série",
    saisons_data="Saisons au format 'S1:lien.com,S2:lien.com' (séparées par des virgules)",
    image="Lien de l'image de la série",
    themes="Genres de la série (séparés par des virgules)"
)
@app_commands.default_permissions(manage_guild=True)
async def importseries(
    interaction: discord.Interaction,
    titre: str,
    saisons_data: str,
    image: str | None = None,
    themes: str | None = None
):
    await interaction.response.defer(ephemeral=False)

    key = titre.lower()
    current_data = load_data("series")
    
    if key not in current_data:
        current_data[key] = {
            "image": image,
            "themes": [g.strip().lower() for g in themes.split(',')] if themes else [],
            "seasons": []
        }
        response_msg_start = f"✅ Nouvelle série **{titre.title()}** créée."
    else:
        response_msg_start = f"✅ Série **{titre.title()}** mise à jour."
        if image:
            current_data[key]["image"] = image
        if themes:
            current_data[key]["themes"] = [g.strip().lower() for g in themes.split(',')]

    added_seasons_count = 0
    updated_seasons_count = 0
    errors = []

    seasons_list = [s.strip() for s in saisons_data.split(',')]
    for season_entry in seasons_list:
        if ':' not in season_entry:
            errors.append(f"Format invalide pour une saison: '{season_entry}'. Attendu 'SX:url'.")
            continue
        
        try:
            season_part, url = season_entry.split(':', 1) 
            
            season_number_str = season_part.strip().upper().replace('S', '')
            if not season_number_str.isdigit():
                errors.append(f"Numéro de saison invalide dans '{season_entry}'. Attendu 'S' suivi d'un nombre.")
                continue

            season_number = int(season_number_str)
            season_url = url.strip()
            season_title = f"Saison {season_number}" 
            
            if not season_url.startswith("http://") and not season_url.startswith("https://"): 
                 errors.append(f"L'URL pour la saison {season_number} ('{season_url}') est invalide. Doit commencer par http:// ou https://.")
                 continue

            season_found = False
            for i, existing_season in enumerate(current_data[key]["seasons"]):
                if existing_season.get("number") == season_number:
                    current_data[key]["seasons"][i]["url"] = season_url
                    current_data[key]["seasons"][i]["title"] = season_title 
                    updated_seasons_count += 1
                    season_found = True
                    break
            
            if not season_found:
                current_data[key]["seasons"].append({
                    "number": season_number,
                    "title": season_title,
                    "url": season_url
                })
                added_seasons_count += 1

        except ValueError:
            errors.append(f"Erreur de conversion du numéro de saison pour '{season_entry}'.")
        except Exception as e:
            errors.append(f"Erreur lors du traitement de la saison '{season_entry}': {e}")
    
    current_data[key]["seasons"].sort(key=lambda s: s.get('number', 0))

    save_data("series", current_data)

    final_response = [response_msg_start]
    if added_seasons_count > 0:
        final_response.append(f"**{added_seasons_count}** saison(s) ajoutée(s).")
    if updated_seasons_count > 0:
        final_response.append(f"**{updated_seasons_count}** saison(s) mise(s) à jour.")
    
    if not added_seasons_count and not updated_seasons_count and not errors:
        final_response.append("Aucune saison ajoutée ou mise à jour. Vérifiez le format de 'saisons_data'.")
    if errors:
        final_response.append("⚠️ Erreur(s) rencontrée(s) :")
        final_response.extend(errors)

    embed = Embed(
        title=f"Import/Mise à jour de la série : {titre.title()}",
        description="\n".join(final_response),
        color=0x1abc9c
    )
    if current_data[key].get("image"): 
        embed.set_image(url=current_data[key]["image"])
    if current_data[key].get("themes"):
        embed.add_field(name="Genres/Thèmes", value=", ".join([t.title() for t in current_data[key]['themes']]), inline=False)
    
    await interaction.followup.send(embed=embed) 
    await update_search_channel_embed(interaction.guild, "series")
    await update_voice_channel_names_for_guild(interaction.guild)

# ---------- Logique de Mise à Jour des Salons Vocaux ----------
async def update_voice_channel_names_for_guild(guild: discord.Guild):
    print(f"Updating voice channel names for guild: {guild.name}")
    for category, channel_base_name in VOICE_CHANNEL_MAP.items():
        current_category_data = load_data(category)
        count = len(current_category_data)

        pattern = re.compile(rf"^{re.escape(channel_base_name)}\s*:\s*\d+$|^{re.escape(channel_base_name)}$", re.IGNORECASE)

        existing_channel = None
        for vc in guild.voice_channels:
            if pattern.match(vc.name):
                existing_channel = vc
                break

        new_name = f"{channel_base_name} : {count}"

        if existing_channel:
            if existing_channel.name != new_name:
                try:
                    await existing_channel.edit(name=new_name)
                    print(f"Updated voice channel '{existing_channel.name}' to '{new_name}' in guild '{guild.name}'.")
                except discord.Forbidden:
                    print(f"❌ Missing permissions to edit voice channel '{existing_channel.name}' in guild '{guild.name}'.")
                except discord.HTTPException as e:
                    print(f"❌ Failed to edit voice channel '{existing_channel.name}' in guild '{guild.name}': {e}")
            else:
                print(f"Voice channel '{new_name}' is already up to date in guild '{guild.name}'.")
        else:
            try:
                await guild.create_voice_channel(new_name)
                print(f"✅ Salon vocal '{new_name}' created for '{category}' in guild '{guild.name}'.")
            except discord.Forbidden:
                print(f"❌ Missing permissions to create voice channel '{new_name}' in guild '{guild.name}'.")
            except Exception as e:
                print(f"❌ Error creating voice channel '{new_name}' in guild '{guild.name}': {e}")

@tasks.loop(minutes=30)
async def periodic_voice_channel_update():
    print("Running periodic voice channel update...")
    for guild in bot.guilds:
        await update_voice_channel_names_for_guild(guild)
    print("Periodic voice channel update finished.")

@periodic_voice_channel_update.before_loop
async def before_periodic_voice_channel_update():
    await bot.wait_until_ready()
    print("Waiting for bot to be ready before starting voice channel update loop...")

# --- Fonction d'aide pour mettre à jour les embeds des salons de recherche ---
async def update_search_channel_embed(guild: discord.Guild, category: str):
    category_channels = {
        "films": "recherche_films",
        "series": "recherche_series",
        "jeux": "recherche_jeux",
        "logiciels": "recherche_logiciels",
    }
    channel_name = category_channels.get(category)
    if channel_name:
        search_channel = discord.utils.get(guild.text_channels, name=channel_name)
        if search_channel:
            search_embed = create_search_embed(category)
            
            search_view = None
            # Déterminer la vue correcte en fonction de la catégorie
            if category == "films":
                search_view = FilmSearchView()
            elif category == "series":
                search_view = SerieSearchView()
            elif category == "jeux":
                search_view = JeuSearchView()
            elif category == "logiciels":
                search_view = LogicielSearchView()
            
            if search_view:
                try:
                    await send_and_cleanup_embed(search_channel, search_embed, search_view, bot.user)
                    print(f"Updated {category.title()} search embed in #{channel_name} for guild {guild.name}.")
                except Exception as e:
                    print(f"❌ Error updating {category.title()} search embed in #{channel_name} for guild {guild.name}: {e}")

# ---------- Événement de Démarrage du Bot (on_ready) ----------
@bot.event
async def on_ready():
    print(f"✅ Bot est connecté à Discord en tant que {bot.user}")

    print("Tentative de synchronisation des commandes slash...")
    try:
        synced_commands = await bot.tree.sync()
        print(f"✅ {len(synced_commands)} commandes slash synchronisées.")
    except Exception as e:
        print(f"❌ Échec de la synchronisation des commandes slash : {e}")

    print("Démarrage de la boucle de mise à jour périodique des salons vocaux...")
    if not periodic_voice_channel_update.is_running():
        periodic_voice_channel_update.start()
        print("Boucle de mise à jour périodique des salons vocaux démarrée.")
    else:
        print("Boucle de mise à jour périodique des salons vocaux déjà en cours.")

    for guild in bot.guilds:
        print(f"Traitement du serveur : {guild.name} (ID: {guild.id})")

        print(f"Vérification/Création/Mise à jour des salons vocaux pour le serveur : {guild.name}")
        await update_voice_channel_names_for_guild(guild)

        demande_channel = discord.utils.get(guild.text_channels, name="demande")
        if demande_channel:
            print(f"Mise en place du salon #demande dans le serveur {guild.name}...")
            ticket_embed = discord.Embed(
                title="Besoin d'aide ?",
                description="Clique sur le bouton ci-dessous pour ouvrir un ticket et obtenir de l'aide.",
                color=0x00ff00
            )
            ticket_view = TicketView()
            try:
                await send_and_cleanup_embed(demande_channel, ticket_embed, ticket_view, bot.user)
                print(f"Embed de ticket mis à jour pour #demande dans {guild.name}.")
            except Exception as e:
                print(f"❌ Erreur lors de la mise en place de #demande dans {guild.name}: {e}")
        else:
            print(f"Le salon texte 'demande' n'a pas été trouvé dans le serveur '{guild.name}'. Ignoré la mise en place.")

        category_channels = {
            "films": "recherche_films",
            "series": "recherche_series",
            "jeux": "recherche_jeux",
            "logiciels": "recherche_logiciels",
        }
        for category, channel_name in category_channels.items():
            search_channel = discord.utils.get(guild.text_channels, name=channel_name)
            if search_channel:
                print(f"Mise en place du salon #{channel_name} dans le serveur {guild.name}...")
                search_embed = create_search_embed(category)
                
                search_view = None
                if category == "films":
                    search_view = FilmSearchView()
                elif category == "series":
                    search_view = SerieSearchView()
                elif category == "jeux":
                    search_view = JeuSearchView()
                elif category == "logiciels":
                    search_view = LogicielSearchView()
                
                if search_view:
                    try:
                        await send_and_cleanup_embed(search_channel, search_embed, search_view, bot.user)
                        print(f"Embed de recherche {category.title()} mis à jour pour #{channel_name} dans {guild.name}.")
                    except Exception as e:
                        print(f"❌ Erreur lors de la mise en place de #{channel_name} dans {guild.name}: {e}")
            else:
                print(f"Le salon de recherche '{channel_name}' n'a pas été trouvé dans le serveur '{guild.name}'. Ignoré la mise en place.")

    print("Le bot est entièrement opérationnel ! Toutes les tâches de configuration ont été tentées.")

# Remplacez "YOUR_BOT_TOKEN_HERE" par le token réel de votre bot
# bot.run("YOUR_BOT_TOKEN_HERE")une variable d'environnement ou un fichier.
keep_alive()
bot.run(token=token)




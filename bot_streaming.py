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

load_dotenv()
token = os.getenv('DISCORD_TOKEN')
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

# --- CONFIGURATION DU SALON D'ARRIVANTS ---
# REMPLACEZ 'NOM_DU_SALON_ACCUEIL' par le nom exact de votre salon de bienvenue (ex: "bienvenue", "general", "accueil")
# Assurez-vous que le bot a les permissions de voir le salon et d'y envoyer des messages.
WELCOME_CHANNEL_NAME = "general" 
# Ou si vous préférez utiliser l'ID direct pour plus de fiabilité :
# WELCOME_CHANNEL_ID = 123456789012345678 # Décommentez et remplacez par l'ID de votre salon
# ------------------------------------------

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
# NOUVELLE LIGNE : Nécessaire pour détecter les arrivées de membres
intents.members = True 
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
        self.data_file_category = data_file_category         # Pour charger les données (films, series, jeux)
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
    
    current_data = load_data(category)
    key = titre.lower()

    if key in current_data:
        return await interaction.followup.send(f"❌ Cet {category[:-1] if category.endswith('s') else category} existe déjà.", ephemeral=True)

    item_data = {
        "url": url,
        "image": image if image else "",
        "themes": [t.strip().lower() for t in themes.split(',')] if themes else [],
        "rating": None, # Initialiser la note à None
        "ratings": [] 
    }
    current_data[key] = item_data
    save_data(category, current_data)
    await interaction.followup.send(f"✅ {titre.title()} ajouté aux {category}!")

# Films
@bot.tree.command(name="addfilm", description="Ajouter un film")
@app_commands.describe(titre="Titre du film", url="Lien du film", image="Lien de l'image (optionnel)", themes="Thèmes séparés par des virgules (action,horreur) (optionnel)")
@app_commands.default_permissions(manage_guild=True)
async def addfilm(interaction: discord.Interaction, titre: str, url: str, image: str | None = None, themes: str | None = None):
    await add_item_command(interaction, "films", titre, url, image, themes)

@bot.tree.command(name="delfilm", description="Supprimer un film")
@app_commands.describe(titre="Titre du film à supprimer")
@app_commands.autocomplete(titre=autocomplete_item_title)
@app_commands.default_permissions(manage_guild=True)
async def delfilm(interaction: discord.Interaction, titre: str):
    await interaction.response.defer(ephemeral=True)
    current_data = load_data("films")
    key = titre.lower()
    if key in current_data:
        del current_data[key]
        save_data("films", current_data)
        await interaction.followup.send(f"✅ Film '{titre.title()}' supprimé.")
    else:
        await interaction.followup.send(f"❌ Film '{titre.title()}' introuvable.")

@bot.tree.command(name="getfilm", description="Obtenir les détails d'un film")
@app_commands.describe(titre="Titre du film")
@app_commands.autocomplete(titre=autocomplete_item_title)
async def getfilm(interaction: discord.Interaction, titre: str):
    current_data = load_data("films")
    key = titre.lower()
    if key in current_data:
        film_info = current_data[key]
        await interaction.response.send_message(
            embed=make_item_embed("films", titre, film_info),
            view=ItemDetailsView("films", titre),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(f"❌ Film '{titre.title()}' introuvable.", ephemeral=True)

# Jeux
@bot.tree.command(name="addjeu", description="Ajouter un jeu")
@app_commands.describe(nom="Nom du jeu", url="Lien du jeu", image="Lien de l'image (optionnel)", themes="Thèmes séparés par des virgules (action,rpg) (optionnel)")
@app_commands.default_permissions(manage_guild=True)
async def addjeu(interaction: discord.Interaction, nom: str, url: str, image: str | None = None, themes: str | None = None):
    await add_item_command(interaction, "jeux", nom, url, image, themes)

@bot.tree.command(name="deljeu", description="Supprimer un jeu")
@app_commands.describe(nom="Nom du jeu à supprimer")
@app_commands.autocomplete(nom=autocomplete_item_title)
@app_commands.default_permissions(manage_guild=True)
async def deljeu(interaction: discord.Interaction, nom: str):
    await interaction.response.defer(ephemeral=True)
    current_data = load_data("jeux")
    key = nom.lower()
    if key in current_data:
        del current_data[key]
        save_data("jeux", current_data)
        await interaction.followup.send(f"✅ Jeu '{nom.title()}' supprimé.")
    else:
        await interaction.followup.send(f"❌ Jeu '{nom.title()}' introuvable.")

@bot.tree.command(name="getjeu", description="Obtenir les détails d'un jeu")
@app_commands.describe(nom="Nom du jeu")
@app_commands.autocomplete(nom=autocomplete_item_title)
async def getjeu(interaction: discord.Interaction, nom: str):
    current_data = load_data("jeux")
    key = nom.lower()
    if key in current_data:
        jeu_info = current_data[key]
        await interaction.response.send_message(
            embed=make_item_embed("jeux", nom, jeu_info),
            view=ItemDetailsView("jeux", nom),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(f"❌ Jeu '{nom.title()}' introuvable.", ephemeral=True)

# Logiciels
@bot.tree.command(name="addlogiciel", description="Ajouter un logiciel")
@app_commands.describe(nom="Nom du logiciel", url="Lien du logiciel", image="Lien de l'image (optionnel)", themes="Thèmes séparés par des virgules (bureautique,graphisme) (optionnel)")
@app_commands.default_permissions(manage_guild=True)
async def addlogiciel(interaction: discord.Interaction, nom: str, url: str, image: str | None = None, themes: str | None = None):
    await add_item_command(interaction, "logiciels", nom, url, image, themes)

@bot.tree.command(name="dellogiciel", description="Supprimer un logiciel")
@app_commands.describe(nom="Nom du logiciel à supprimer")
@app_commands.autocomplete(nom=autocomplete_item_title)
@app_commands.default_permissions(manage_guild=True)
async def dellogiciel(interaction: discord.Interaction, nom: str):
    await interaction.response.defer(ephemeral=True)
    current_data = load_data("logiciels")
    key = nom.lower()
    if key in current_data:
        del current_data[key]
        save_data("logiciels", current_data)
        await interaction.followup.send(f"✅ Logiciel '{nom.title()}' supprimé.")
    else:
        await interaction.followup.send(f"❌ Logiciel '{nom.title()}' introuvable.")

@bot.tree.command(name="getlogiciel", description="Obtenir les détails d'un logiciel")
@app_commands.describe(nom="Nom du logiciel")
@app_commands.autocomplete(nom=autocomplete_item_title)
async def getlogiciel(interaction: discord.Interaction, nom: str):
    current_data = load_data("logiciels")
    key = nom.lower()
    if key in current_data:
        logiciel_info = current_data[key]
        await interaction.response.send_message(
            embed=make_item_embed("logiciels", nom, logiciel_info),
            view=ItemDetailsView("logiciels", nom),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(f"❌ Logiciel '{nom.title()}' introuvable.", ephemeral=True)

# Séries (gestion des saisons)
@bot.tree.command(name="addserieseason", description="Ajouter ou modifier une série et une saison")
@app_commands.describe(titre="Titre de la série", saison_numero="Numéro de la saison", saison_url="Lien de la saison", image="Lien de l'image (optionnel)", themes="Thèmes séparés par des virgules (drame,sci-fi) (optionnel)")
@app_commands.default_permissions(manage_guild=True)
async def addserieseason(interaction: discord.Interaction, titre: str, saison_numero: int, saison_url: str, image: str | None = None, themes: str | None = None):
    await interaction.response.defer(ephemeral=False)
    
    current_data = load_data("series")
    key = titre.lower()

    # Si la série n'existe pas, la créer
    if key not in current_data:
        current_data[key] = {
            "image": image if image else "",
            "themes": [t.strip().lower() for t in themes.split(',')] if themes else [],
            "rating": None,
            "ratings": [],
            "seasons": []
        }
    else:
        # Si la série existe, mettre à jour l'image et les thèmes si fournis
        if image:
            current_data[key]["image"] = image
        if themes:
            current_data[key]["themes"] = [t.strip().lower() for t in themes.split(',')]

    # Vérifier si la saison existe déjà
    season_exists = False
    for season in current_data[key]["seasons"]:
        if season.get("number") == saison_numero:
            season["url"] = saison_url
            season["title"] = f"Saison {saison_numero}" # Mettre à jour le titre de la saison
            season_exists = True
            break
    
    if not season_exists:
        current_data[key]["seasons"].append({
            "number": saison_numero,
            "title": f"Saison {saison_numero}",
            "url": saison_url
        })
    
    save_data("series", current_data)
    await interaction.followup.send(f"✅ Série '{titre.title()}' - Saison {saison_numero} ajoutée/modifiée avec succès!")

@bot.tree.command(name="delserieseason", description="Supprimer une saison spécifique d'une série")
@app_commands.describe(titre="Titre de la série", saison_numero="Numéro de la saison à supprimer")
@app_commands.autocomplete(titre=autocomplete_item_title)
@app_commands.default_permissions(manage_guild=True)
async def delserieseason(interaction: discord.Interaction, titre: str, saison_numero: int):
    await interaction.response.defer(ephemeral=True)
    current_data = load_data("series")
    key = titre.lower()

    if key not in current_data:
        return await interaction.followup.send(f"❌ Série '{titre.title()}' introuvable.")
    
    original_season_count = len(current_data[key].get("seasons", []))
    current_data[key]["seasons"] = [
        s for s in current_data[key].get("seasons", []) if s.get("number") != saison_numero
    ]

    if len(current_data[key]["seasons"]) < original_season_count:
        if not current_data[key]["seasons"] and not current_data[key].get("image") and not current_data[key].get("themes"):
            # Si plus aucune saison, image ou thèmes, supprimer la série entière
            del current_data[key]
            save_data("series", current_data)
            await interaction.followup.send(f"✅ Série '{titre.title()}' (et sa dernière saison) supprimée car il n'y avait plus de contenu lié.")
        else:
            save_data("series", current_data)
            await interaction.followup.send(f"✅ Saison {saison_numero} de la série '{titre.title()}' supprimée.")
    else:
        await interaction.followup.send(f"❌ Saison {saison_numero} de la série '{titre.title()}' introuvable.")

@bot.tree.command(name="delseries", description="Supprimer une série entière (toutes les saisons)")
@app_commands.describe(titre="Titre de la série à supprimer")
@app_commands.autocomplete(titre=autocomplete_item_title)
@app_commands.default_permissions(manage_guild=True)
async def delseries(interaction: discord.Interaction, titre: str):
    await interaction.response.defer(ephemeral=True)
    current_data = load_data("series")
    key = titre.lower()
    if key in current_data:
        del current_data[key]
        save_data("series", current_data)
        await interaction.followup.send(f"✅ Série '{titre.title()}' (toutes les saisons) supprimée.")
    else:
        await interaction.followup.send(f"❌ Série '{titre.title()}' introuvable.")

@bot.tree.command(name="getserie", description="Obtenir les détails d'une série")
@app_commands.describe(titre="Titre de la série")
@app_commands.autocomplete(titre=autocomplete_item_title)
async def getserie(interaction: discord.Interaction, titre: str):
    current_data = load_data("series")
    key = titre.lower()
    if key in current_data:
        serie_info = current_data[key]
        await interaction.response.send_message(
            embed=make_item_embed("series", titre, serie_info),
            view=ItemDetailsView("series", titre),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(f"❌ Série '{titre.title()}' introuvable.", ephemeral=True)

# ---------- Importation de données (staff) ----------
@bot.tree.command(name="importfilms", description="Importer un fichier JSON de films (Staff)")
@app_commands.describe(file="Le fichier JSON à importer")
@app_commands.default_permissions(manage_guild=True)
async def importfilms(interaction: discord.Interaction, file: discord.Attachment):
    await import_data_command(interaction, "films", file)

@bot.tree.command(name="importseries", description="Importer un fichier JSON de séries (Staff)")
@app_commands.describe(file="Le fichier JSON à importer")
@app_commands.default_permissions(manage_guild=True)
async def importseries(interaction: discord.Interaction, file: discord.Attachment):
    await import_data_command(interaction, "series", file)

@bot.tree.command(name="importjeux", description="Importer un fichier JSON de jeux (Staff)")
@app_commands.describe(file="Le fichier JSON à importer")
@app_commands.default_permissions(manage_guild=True)
async def importjeux(interaction: discord.Interaction, file: discord.Attachment):
    await import_data_command(interaction, "jeux", file)

@bot.tree.command(name="importlogiciels", description="Importer un fichier JSON de logiciels (Staff)")
@app_commands.describe(file="Le fichier JSON à importer")
@app_commands.default_permissions(manage_guild=True)
async def importlogiciels(interaction: discord.Interaction, file: discord.Attachment):
    await import_data_command(interaction, "logiciels", file)

async def import_data_command(interaction: discord.Interaction, category: str, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)
    if not file.filename.endswith(".json"):
        return await interaction.followup.send("❌ Veuillez n'importer que des fichiers JSON.", ephemeral=True)

    try:
        data_bytes = await file.read()
        new_data = json.loads(data_bytes.decode('utf-8'))

        current_data = load_data(category)
        
        # Merge new data into existing data
        for key, value in new_data.items():
            current_data[key.lower()] = value
        
        save_data(category, current_data)
        await interaction.followup.send(f"✅ Données pour les {category} importées et fusionnées avec succès!")
    except json.JSONDecodeError:
        await interaction.followup.send("❌ Le fichier JSON est invalide.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Une erreur est survenue lors de l'importation: {e}", ephemeral=True)

# ---------- Événements du Bot ----------
@bot.event
async def on_ready():
    print(f'{bot.user.name} est connecté et prêt !')
    try:
        synced = await bot.tree.sync()
        print(f"Synchronisation de {len(synced)} commandes slash.")
    except Exception as e:
        print(f"Erreur lors de la synchronisation des commandes slash : {e}")

    # Relancer les vues persistantes
    bot.add_view(FilmSearchView())
    bot.add_view(SerieSearchView())
    bot.add_view(JeuSearchView())
    bot.add_view(LogicielSearchView())
    bot.add_view(TicketView())
    bot.add_view(TicketCloseView())

# NOUVELLE FONCTION : Gestionnaire d'événement pour les nouveaux membres
@bot.event
async def on_member_join(member):
    """
    Se déclenche lorsqu'un nouveau membre rejoint le serveur.
    """
    # Tente de trouver le salon d'accueil par son nom
    welcome_channel = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL_NAME)
    
    # Si vous avez défini WELCOME_CHANNEL_ID, utilisez-le pour plus de fiabilité
    # welcome_channel = bot.get_channel(WELCOME_CHANNEL_ID) 

    if welcome_channel:
        embed = discord.Embed(
            title=f"🎉 Bienvenue sur Netflips, {member.name} !",
            description=f"Nous sommes ravis de te compter parmi nous, {member.mention} !\n"
                        "N'hésite pas à explorer nos catalogues de films, séries, jeux et logiciels.",
            color=discord.Color.from_rgb(229, 9, 20) # Rouge Netflips (hex #e50914)
        )
        
        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)
        else:
            embed.set_thumbnail(url=member.default_avatar.url)

        embed.set_footer(text=f"A rejoint le serveur le {member.joined_at.strftime('%d/%m/%Y à %H:%M')}")
        
        try:
            await welcome_channel.send(embed=embed)
        except discord.Forbidden:
            print(f"Erreur: Je n'ai pas les permissions d'envoyer des messages dans le salon '{welcome_channel.name}'.")
        except Exception as e:
            print(f"Erreur lors de l'envoi du message de bienvenue : {e}")
    else:
        print(f"AVERTISSEMENT : Le salon de bienvenue '{WELCOME_CHANNEL_NAME}' n'a pas été trouvé. Veuillez vérifier la constante WELCOME_CHANNEL_NAME.")

# ---------- Lancement du Bot ----------
# Assurez-vous que votre TOKEN est défini quelque part, par exemple via une variable d'environnement ou dans un fichier config.py
# Exemple : bot.run(os.getenv("DISCORD_TOKEN"))
# Remplacez ceci par la ligne qui lance votre bot avec votre token
# bot.run("VOTRE_TOKEN_ICI") # Mettez votre token ici si vous ne le chargez pas depuis une variable d'environnement ou un fichier.
keep_alive()
bot.run(token=token)


